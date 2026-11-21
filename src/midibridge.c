// midibridge - a line-oriented CoreMIDI SysEx pipe.
//
//   stdin : a hex string to send, "r" to just read the next message, or
//           "s<hex>" to fire a short MIDI message (transport, notes) and move
//           on -- those get no reply, so waiting for one would only stall
//   stdout: one complete SysEx message per line, as hex ("" on timeout)
//
// Incoming bytes are buffered and handed out one complete F0..F7 message at a
// time, so a late or unsolicited message never swallows the reply after it --
// the caller matches replies by request id and skips the rest.
#include <CoreMIDI/CoreMIDI.h>
#include <CoreFoundation/CoreFoundation.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/time.h>
#include <pthread.h>

#define RXCAP (1 << 22)
static unsigned char rx[RXCAP];
static volatile size_t rxlen = 0;   // bytes received
static size_t consumed = 0;         // bytes already handed to the caller

/* the buffer is shared with CoreMIDI's thread: one lock for appending, taking and resetting */
static pthread_mutex_t mx = PTHREAD_MUTEX_INITIALIZER;
static int insx = 0;                // inside a SysEx right now
static size_t sxstart = 0;          // where it started

/* Keep SysEx only. Real-time bytes (clock, start/stop) may sit inside a SysEx and are dropped;
   notes and other messages between SysEx are not needed; a SysEx cut short (a new F0, or a
   status byte before its F7) is thrown away so it can't glue onto the next reply. */
static void readProc(const MIDIPacketList *pl, void *a, void *b) {
    (void)a; (void)b;
    pthread_mutex_lock(&mx);
    const MIDIPacket *p = &pl->packet[0];
    for (unsigned i = 0; i < pl->numPackets; i++) {
        for (unsigned j = 0; j < p->length; j++) {
            unsigned char c = p->data[j];
            if (c >= 0xF8) continue;
            if (c == 0xF0) { if (insx) rxlen = sxstart; insx = 1; sxstart = rxlen; }
            else if (!insx) continue;
            else if ((c & 0x80) && c != 0xF7) { rxlen = sxstart; insx = 0; continue; }
            if (rxlen < RXCAP) rx[rxlen++] = c;
            if (c == 0xF7) insx = 0;
        }
        p = MIDIPacketNext(p);
    }
    pthread_mutex_unlock(&mx);
}

static MIDIEndpointRef findEP(const char *want, int dest) {
    ItemCount n = dest ? MIDIGetNumberOfDestinations() : MIDIGetNumberOfSources();
    for (ItemCount i = 0; i < n; i++) {
        MIDIEndpointRef e = dest ? MIDIGetDestination(i) : MIDIGetSource(i);
        CFStringRef nm = NULL; char buf[256] = "";
        MIDIObjectGetStringProperty(e, kMIDIPropertyDisplayName, &nm);
        if (nm) CFStringGetCString(nm, buf, sizeof buf, kCFStringEncodingUTF8);
        if (strstr(buf, want)) return e;
    }
    return 0;
}

/* Hand back the next complete F0..F7 message, or 0 if none is buffered yet. */
static int take_message(size_t *start, size_t *end) {
    pthread_mutex_lock(&mx);
    size_t i = consumed, have = rxlen;
    while (i < have && rx[i] != 0xF0) i++;
    if (i >= have) { consumed = have; pthread_mutex_unlock(&mx); return 0; }
    size_t j = i;
    while (j < have && rx[j] != 0xF7) j++;
    if (j >= have) { pthread_mutex_unlock(&mx); return 0; }   // incomplete, wait for more
    *start = i; *end = j + 1;
    consumed = j + 1;
    pthread_mutex_unlock(&mx);
    return 1;
}

static long elapsed_us(struct timeval *t0) {
    struct timeval now; gettimeofday(&now, NULL);
    return (now.tv_sec - t0->tv_sec) * 1000000L + (now.tv_usec - t0->tv_usec);
}

int main(int argc, char **argv) {
    if (argc < 2) { fprintf(stderr, "usage: %s <dev-substr>\n", argv[0]); return 2; }
    MIDIClientRef cl; MIDIPortRef in, out;
    MIDIClientCreate(CFSTR("bridge"), NULL, NULL, &cl);
    MIDIInputPortCreate(cl, CFSTR("in"), readProc, NULL, &in);
    MIDIOutputPortCreate(cl, CFSTR("out"), &out);
    MIDIEndpointRef src = findEP(argv[1], 0), dst = findEP(argv[1], 1);
    if (!src || !dst) { fprintf(stderr, "device not found\n"); return 1; }
    MIDIPortConnectSource(in, src, NULL);
    fprintf(stderr, "READY\n");

    char line[1 << 18];
    while (fgets(line, sizeof line, stdin)) {
        int readonly = (line[0] == 'r' || line[0] == 'R');
        int sendonly = (line[0] == 's' || line[0] == 'S');
        long timeout_us = readonly ? 400000L : 6000000L;

        /* short message, no reply expected: send it and acknowledge at once */
        if (sendonly) {
            const char *hex = line + 1;
            size_t hl = strspn(hex, "0123456789abcdefABCDEF"), mlen = hl / 2;
            if (mlen > 0) {
                if (mlen > 64) mlen = 64;
                unsigned char msg[64];
                for (size_t i = 0; i < mlen; i++) {
                    unsigned v; sscanf(hex + 2*i, "%2x", &v); msg[i] = (unsigned char)v;
                }
                Byte buf[512];
                MIDIPacketList *pl = (MIDIPacketList *)buf;
                MIDIPacket *pk = MIDIPacketListInit(pl);
                MIDIPacketListAdd(pl, sizeof buf, pk, 0, mlen, msg);
                MIDISend(out, dst, pl);
            }
            printf("\n");
            fflush(stdout);
            continue;
        }

        if (!readonly) {
            size_t hl = strspn(line, "0123456789abcdefABCDEF");
            if (hl < 2) continue;
            size_t mlen = hl / 2;
            unsigned char *msg = malloc(mlen);
            for (size_t i = 0; i < mlen; i++) {
                unsigned v; sscanf(line + 2*i, "%2x", &v); msg[i] = (unsigned char)v;
            }
            /* MIDISendSysex paces itself at DIN-MIDI speed (~3 KB/s), which makes a
               2 MB upload take a quarter of an hour. The EP is a USB device, so
               hand CoreMIDI the whole message as packets and let USB run at its
               own speed: 256-byte packets in one list, sent in one call. */
            size_t cap = mlen + (mlen / 256 + 2) * 64 + 1024;
            Byte *buf = malloc(cap);
            MIDIPacketList *pl = (MIDIPacketList *)buf;
            MIDIPacket *pk = MIDIPacketListInit(pl);
            for (size_t off = 0; off < mlen && pk; off += 256) {
                size_t n = mlen - off < 256 ? mlen - off : 256;
                pk = MIDIPacketListAdd(pl, cap, pk, 0, n, msg + off);
            }
            if (pk) MIDISend(out, dst, pl);
            else {                                   /* list overflow: fall back */
                MIDISysexSendRequest req = {0};
                req.destination = dst; req.data = msg; req.bytesToSend = (UInt32)mlen;
                MIDISendSysex(&req);
                while (!req.complete) usleep(200);
            }
            free(buf);
            free(msg);
        }

        /* readProc runs on CoreMIDI's own thread, so usleep (which yields) is
           correct here -- spinning a run loop starves that thread. */
        struct timeval t0; gettimeofday(&t0, NULL);
        size_t s = 0, e = 0;
        int got = 0;
        for (;;) {
            if (take_message(&s, &e)) { got = 1; break; }
            if (elapsed_us(&t0) > timeout_us) break;
            usleep(200);
        }

        if (got) for (size_t i = s; i < e; i++) printf("%02x", rx[i]);
        printf("\n");
        fflush(stdout);

        /* reclaim the buffer once everything received has been handed out */
        pthread_mutex_lock(&mx);
        if (consumed >= rxlen && !insx) { rxlen = 0; consumed = 0; sxstart = 0; }
        pthread_mutex_unlock(&mx);
    }
    return 0;
}
