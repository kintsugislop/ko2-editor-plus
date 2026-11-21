// usbrec - record from a CoreAudio input device to a WAV file, no dependencies.
//
//   usbrec --list                       list input-capable devices
//   usbrec --meter "EP-133" [secs]       open + measure levels, write nothing
//   usbrec --rec "EP-133" out.wav [secs] record until secs elapse or SIGINT
//
// When stderr is not a tty it prints one METER line per tick carrying peak, rms
// and a BANDS spectrum, so a supervising process can drive a live display.
//
#include <AudioUnit/AudioUnit.h>
#include <CoreAudio/CoreAudio.h>
#include <CoreFoundation/CoreFoundation.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <math.h>
#include <pthread.h>

static volatile sig_atomic_t running = 1;
static void on_sig(int s) { (void)s; running = 0; }

typedef struct {
    short *history;
    size_t capacity, captured;
    pthread_mutex_t history_lock;
    FILE *f;              // NULL in meter mode
    AudioBufferList *abl;
    AudioUnit unit;
    UInt32 channels;
    unsigned long long frames;
    float peak;
    double sumsq;
    unsigned long long nsamp;
} Rec;

/* ---------- spectrum analyser ----------
   The capture callback drops a mono mix into a ring; the main loop windows the
   most recent ANA_N samples and folds the FFT into log-spaced bands. Nothing
   here blocks the audio thread -- a torn read just costs one ugly frame. */

#define RING_N  8192                 /* power of two */
#define ANA_N   1024                 /* FFT size */
#define NBANDS  20
#define DB_FLOOR (-72.0)

static float ring[RING_N];
static volatile unsigned long ringw = 0;

static void ring_push(float v) { ring[ringw & (RING_N - 1)] = v; ringw++; }

static void fft(float *re, float *im, int n) {
    for (int i = 1, j = 0; i < n; i++) {          /* bit-reversal permutation */
        int bit = n >> 1;
        for (; j & bit; bit >>= 1) j ^= bit;
        j ^= bit;
        if (i < j) {
            float t = re[i]; re[i] = re[j]; re[j] = t;
            t = im[i]; im[i] = im[j]; im[j] = t;
        }
    }
    for (int len = 2; len <= n; len <<= 1) {
        double ang = -2.0 * M_PI / len;
        int half = len >> 1;
        for (int i = 0; i < n; i += len) {
            for (int k = 0; k < half; k++) {
                double c = cos(ang * k), sn = sin(ang * k);
                float xr = re[i + k + half], xi = im[i + k + half];
                float vr = (float)(xr * c - xi * sn);
                float vi = (float)(xr * sn + xi * c);
                float ur = re[i + k], ui = im[i + k];
                re[i + k] = ur + vr;        im[i + k] = ui + vi;
                re[i + k + half] = ur - vr; im[i + k + half] = ui - vi;
            }
        }
    }
}

/* Fill out[NBANDS] with 0..1 band levels for the newest ANA_N samples. */
static void spectrum(double sr, float *out) {
    static float re[ANA_N], im[ANA_N];
    unsigned long w = ringw;
    for (int i = 0; i < ANA_N; i++) {
        float s = (w >= ANA_N) ? ring[(w - ANA_N + i) & (RING_N - 1)] : 0.0f;
        float win = 0.5f - 0.5f * cosf(2.0f * (float)M_PI * i / (ANA_N - 1));
        re[i] = s * win;
        im[i] = 0.0f;
    }
    fft(re, im, ANA_N);

    double lo = 40.0, hi = sr / 2.0 > 16000.0 ? 16000.0 : sr / 2.0;
    if (hi <= lo) hi = lo * 2;
    double ratio = log(hi / lo) / NBANDS;
    for (int b = 0; b < NBANDS; b++) {
        double f0 = lo * exp(ratio * b), f1 = lo * exp(ratio * (b + 1));
        int k0 = (int)(f0 * ANA_N / sr), k1 = (int)(f1 * ANA_N / sr);
        if (k1 <= k0) k1 = k0 + 1;
        if (k1 > ANA_N / 2) k1 = ANA_N / 2;
        double sum = 0; int n = 0;
        for (int k = k0; k < k1; k++) {
            double m = (double)re[k] * re[k] + (double)im[k] * im[k];
            sum += m; n++;
        }
        double mag = n ? sqrt(sum / n) * 2.0 / (ANA_N * 0.5) : 0.0;
        double db = mag > 1e-9 ? 20.0 * log10(mag) : DB_FLOOR;
        double v = (db - DB_FLOOR) / -DB_FLOOR;
        out[b] = (float)(v < 0 ? 0 : v > 1 ? 1 : v);
    }
}

static void die(const char *what, OSStatus err) {
    fprintf(stderr, "%s failed (%d)\n", what, (int)err);
    exit(1);
}

/* ---------- device enumeration ---------- */

static void dev_name(AudioObjectID d, AudioObjectPropertySelector sel, char *out, int n) {
    CFStringRef s = NULL; UInt32 sz = sizeof s; out[0] = 0;
    AudioObjectPropertyAddress a = {sel, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain};
    if (AudioObjectGetPropertyData(d, &a, 0, NULL, &sz, &s) == noErr && s) {
        CFStringGetCString(s, out, n, kCFStringEncodingUTF8); CFRelease(s);
    }
}

static int dev_channels(AudioObjectID d, int input) {
    AudioObjectPropertyAddress a = {kAudioDevicePropertyStreamConfiguration,
        input ? kAudioObjectPropertyScopeInput : kAudioObjectPropertyScopeOutput,
        kAudioObjectPropertyElementMain};
    UInt32 sz = 0;
    if (AudioObjectGetPropertyDataSize(d, &a, 0, NULL, &sz) != noErr) return 0;
    AudioBufferList *bl = malloc(sz); int n = 0;
    if (AudioObjectGetPropertyData(d, &a, 0, NULL, &sz, bl) == noErr)
        for (UInt32 i = 0; i < bl->mNumberBuffers; i++) n += bl->mBuffers[i].mNumberChannels;
    free(bl); return n;
}

static AudioObjectID find_input(const char *want, char *nameout, int nlen, int *chout) {
    AudioObjectPropertyAddress a = {kAudioHardwarePropertyDevices,
        kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain};
    UInt32 sz = 0;
    AudioObjectGetPropertyDataSize(kAudioObjectSystemObject, &a, 0, NULL, &sz);
    int n = sz / sizeof(AudioObjectID);
    AudioObjectID *ds = malloc(sz);
    AudioObjectGetPropertyData(kAudioObjectSystemObject, &a, 0, NULL, &sz, ds);
    AudioObjectID found = kAudioObjectUnknown;
    for (int i = 0; i < n; i++) {
        char nm[256]; dev_name(ds[i], kAudioObjectPropertyName, nm, sizeof nm);
        int in = dev_channels(ds[i], 1);
        if (in > 0 && (!want || strstr(nm, want))) {
            found = ds[i];
            if (nameout) snprintf(nameout, nlen, "%s", nm);
            if (chout) *chout = in;
            break;
        }
    }
    free(ds); return found;
}

static void list_devices(void) {
    AudioObjectPropertyAddress a = {kAudioHardwarePropertyDevices,
        kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain};
    UInt32 sz = 0;
    AudioObjectGetPropertyDataSize(kAudioObjectSystemObject, &a, 0, NULL, &sz);
    int n = sz / sizeof(AudioObjectID);
    AudioObjectID *ds = malloc(sz);
    AudioObjectGetPropertyData(kAudioObjectSystemObject, &a, 0, NULL, &sz, ds);
    printf("input-capable devices:\n");
    int any = 0;
    for (int i = 0; i < n; i++) {
        int in = dev_channels(ds[i], 1);
        if (!in) continue;
        char nm[256], uid[256];
        dev_name(ds[i], kAudioObjectPropertyName, nm, sizeof nm);
        dev_name(ds[i], kAudioDevicePropertyDeviceUID, uid, sizeof uid);
        Float64 sr = 0; UInt32 s2 = sizeof sr;
        AudioObjectPropertyAddress ra = {kAudioDevicePropertyNominalSampleRate,
            kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain};
        AudioObjectGetPropertyData(ds[i], &ra, 0, NULL, &s2, &sr);
        printf("  %-32s  in=%-2d  %.0f Hz   uid=%s\n", nm, in, sr, uid);
        any = 1;
    }
    if (!any) printf("  (none)\n");
    free(ds);
}

/* ---------- WAV ---------- */

static void wav_header(FILE *f, int ch, int sr, unsigned long long databytes) {
    unsigned int block = ch * 2, brate = sr * block;
    unsigned int riff = (unsigned int)(36 + databytes), dsz = (unsigned int)databytes;
    fwrite("RIFF", 1, 4, f); fwrite(&riff, 4, 1, f);
    fwrite("WAVEfmt ", 1, 8, f);
    unsigned int sub1 = 16; unsigned short fmt = 1, nch = ch, bits = 16;
    fwrite(&sub1, 4, 1, f); fwrite(&fmt, 2, 1, f); fwrite(&nch, 2, 1, f);
    unsigned int s = sr; fwrite(&s, 4, 1, f); fwrite(&brate, 4, 1, f);
    unsigned short blk = block; fwrite(&blk, 2, 1, f); fwrite(&bits, 2, 1, f);
    fwrite("data", 1, 4, f); fwrite(&dsz, 4, 1, f);
}

/* Keep an extra second so small snapshot reads can release the callback lock. */
static volatile sig_atomic_t keep_requested;
static void request_keep(int sig) { (void)sig; keep_requested = 1; }

static void remember(Rec *r, const short *pcm, size_t frames) {
    pthread_mutex_lock(&r->history_lock);
    size_t at = r->captured % r->capacity;
    size_t first = frames < r->capacity-at ? frames : r->capacity-at;
    size_t stride = r->channels * sizeof(short);
    memcpy(r->history + at*r->channels, pcm, first*stride);
    memcpy(r->history, pcm + first*r->channels, (frames-first)*stride);
    r->captured += frames;
    pthread_mutex_unlock(&r->history_lock);
}

static void snapshot(Rec *r, int rate) {
    pthread_mutex_lock(&r->history_lock);
    size_t end = r->captured;
    pthread_mutex_unlock(&r->history_lock);
    size_t frames = end < (size_t)rate*30 ? end : (size_t)rate*30;
    size_t start = end-frames, stride = r->channels*sizeof(short);
    char *copy = malloc(frames*stride);
    if (!copy) frames = 0;
    for (size_t i = 0; i < frames;) {
        size_t at = (start+i) % r->capacity, n = frames-i;
        if (n > 4096) n = 4096;
        if (n > r->capacity-at) n = r->capacity-at;
        pthread_mutex_lock(&r->history_lock);
        if (r->captured-(start+i) > r->capacity) frames = 0;
        else memcpy(copy+i*stride, r->history+at*r->channels, n*stride);
        pthread_mutex_unlock(&r->history_lock);
        i += n;
    }
    wav_header(stdout, r->channels, rate, frames*stride);
    if (frames) fwrite(copy, stride, frames, stdout);
    fflush(stdout);
    free(copy);
}

/* ---------- capture callback ---------- */

static OSStatus on_input(void *ref, AudioUnitRenderActionFlags *flags,
                         const AudioTimeStamp *ts, UInt32 bus, UInt32 nframes,
                         AudioBufferList *unused) {
    (void)unused;
    Rec *r = (Rec *)ref;
    r->abl->mNumberBuffers = 1;
    r->abl->mBuffers[0].mNumberChannels = r->channels;
    r->abl->mBuffers[0].mDataByteSize = nframes * r->channels * 2;
    OSStatus e = AudioUnitRender(r->unit, flags, ts, bus, nframes, r->abl);
    if (e != noErr) return e;
    short *pcm = (short *)r->abl->mBuffers[0].mData;
    UInt32 n = nframes * r->channels;
    for (UInt32 i = 0; i < n; i++) {
        float v = pcm[i] / 32768.0f;
        float av = fabsf(v);
        if (av > r->peak) r->peak = av;
        r->sumsq += (double)v * v;
    }
    for (UInt32 f = 0; f < nframes; f++) {            /* mono mix -> analyser */
        float acc = 0;
        for (UInt32 c = 0; c < r->channels; c++) acc += pcm[f * r->channels + c] / 32768.0f;
        ring_push(acc / (float)r->channels);
    }
    r->nsamp += n;
    r->frames += nframes;
    if (r->history) remember(r, pcm, nframes);
    if (r->f) fwrite(pcm, 1, nframes * r->channels * 2, r->f);
    return noErr;
}

/* ---------- main ---------- */

int main(int argc, char **argv) {
    if (argc < 2 || !strcmp(argv[1], "--help")) {
        fprintf(stderr, "usage:\n  %s --list\n  %s --meter <device> [secs]\n"
                        "  %s --rec <device> <out.wav> [secs]\n", argv[0], argv[0], argv[0]);
        return 2;
    }
    if (!strcmp(argv[1], "--list")) { list_devices(); return 0; }

    int history = !strcmp(argv[1], "--history");
    int meter = history || !strcmp(argv[1], "--meter");
    if (!meter && strcmp(argv[1], "--rec")) { fprintf(stderr, "unknown mode %s\n", argv[1]); return 2; }
    if (argc < 3) { fprintf(stderr, "need a device name\n"); return 2; }

    const char *want = argv[2];
    const char *outpath = meter ? NULL : (argc > 3 ? argv[3] : "out.wav");
    double secs = 0;
    if (meter) secs = argc > 3 ? atof(argv[3]) : 3.0;
    else       secs = argc > 4 ? atof(argv[4]) : 0.0;   /* 0 = until SIGINT */

    char nm[256] = ""; int nch = 0;
    AudioObjectID dev = find_input(want, nm, sizeof nm, &nch);
    if (dev == kAudioObjectUnknown) {
        fprintf(stderr, "no input device matching \"%s\"\n", want);
        list_devices();
        return 1;
    }

    Float64 sr = 48000; UInt32 sz = sizeof sr;
    AudioObjectPropertyAddress ra = {kAudioDevicePropertyNominalSampleRate,
        kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain};
    AudioObjectGetPropertyData(dev, &ra, 0, NULL, &sz, &sr);

    /* AUHAL in input mode */
    AudioComponentDescription desc = {kAudioUnitType_Output, kAudioUnitSubType_HALOutput,
                                      kAudioUnitManufacturer_Apple, 0, 0};
    AudioComponent comp = AudioComponentFindNext(NULL, &desc);
    if (!comp) { fprintf(stderr, "no HAL audio unit\n"); return 1; }
    AudioUnit unit; OSStatus e;
    if ((e = AudioComponentInstanceNew(comp, &unit))) die("AudioComponentInstanceNew", e);

    UInt32 on = 1, off = 0;
    if ((e = AudioUnitSetProperty(unit, kAudioOutputUnitProperty_EnableIO, kAudioUnitScope_Input,  1, &on,  sizeof on)))  die("enable input", e);
    if ((e = AudioUnitSetProperty(unit, kAudioOutputUnitProperty_EnableIO, kAudioUnitScope_Output, 0, &off, sizeof off))) die("disable output", e);
    if ((e = AudioUnitSetProperty(unit, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global, 0, &dev, sizeof dev))) die("set device", e);

    AudioStreamBasicDescription fmt = {0};
    fmt.mSampleRate = sr;
    fmt.mFormatID = kAudioFormatLinearPCM;
    fmt.mFormatFlags = kAudioFormatFlagIsSignedInteger | kAudioFormatFlagIsPacked;
    fmt.mBitsPerChannel = 16;
    fmt.mChannelsPerFrame = nch;
    fmt.mFramesPerPacket = 1;
    fmt.mBytesPerFrame = fmt.mBytesPerPacket = 2 * nch;
    if ((e = AudioUnitSetProperty(unit, kAudioUnitProperty_StreamFormat, kAudioUnitScope_Output, 1, &fmt, sizeof fmt)))
        die("set client format", e);

    Rec rec = {0};
    rec.unit = unit; rec.channels = nch; rec.peak = 0;
    rec.abl = malloc(sizeof(AudioBufferList) + sizeof(AudioBuffer));
    rec.abl->mNumberBuffers = 1;
    rec.abl->mBuffers[0].mData = malloc(65536 * 2 * nch);

    if (history) {
        rec.capacity = 31 * (size_t)sr;
        rec.history = calloc(rec.capacity, nch * sizeof(short));
        if (!rec.history || pthread_mutex_init(&rec.history_lock, NULL)) return 1;
        signal(SIGUSR1, request_keep);
    }
    if (!meter) {
        rec.f = fopen(outpath, "wb");
        if (!rec.f) { perror("open output"); return 1; }
        wav_header(rec.f, nch, (int)sr, 0);
    }

    AURenderCallbackStruct cb = {on_input, &rec};
    if ((e = AudioUnitSetProperty(unit, kAudioOutputUnitProperty_SetInputCallback, kAudioUnitScope_Global, 0, &cb, sizeof cb)))
        die("set callback", e);
    if ((e = AudioUnitInitialize(unit))) die("AudioUnitInitialize", e);

    signal(SIGINT, on_sig); signal(SIGTERM, on_sig);
    if ((e = AudioOutputUnitStart(unit))) die("AudioOutputUnitStart", e);

    fprintf(stderr, "%s  \"%s\"  %d ch @ %.0f Hz%s\n",
            meter ? "metering" : "recording", nm, nch, sr,
            meter ? "  (writing nothing)" : "");

    double elapsed = 0;
    while (running && (secs <= 0 || elapsed < secs)) {
        if (history && keep_requested) { keep_requested = 0; snapshot(&rec, (int)sr); }
        usleep(100000); elapsed += 0.1;
        double rms = rec.nsamp ? sqrt(rec.sumsq / rec.nsamp) : 0;
        if (isatty(2))
            fprintf(stderr, "\r  %6.1fs  frames=%-10llu peak=%-6.4f rms=%-6.4f",
                    elapsed, rec.frames, rec.peak, rms);
        else {
            /* line-oriented so a supervising process can parse it */
            float bands[NBANDS];
            spectrum(sr, bands);
            fprintf(stderr, "METER secs=%.1f frames=%llu peak=%.5f rms=%.5f bands=",
                    elapsed, rec.frames, rec.peak, rms);
            for (int b = 0; b < NBANDS; b++)
                fprintf(stderr, "%s%.3f", b ? "," : "", bands[b]);
            fprintf(stderr, "\n");
            fflush(stderr);
        }
        rec.peak = 0; rec.sumsq = 0; rec.nsamp = 0;
    }
    fprintf(stderr, "\n");

    AudioOutputUnitStop(unit);
    AudioUnitUninitialize(unit);
    AudioComponentInstanceDispose(unit);

    if (rec.f) {
        unsigned long long databytes = rec.frames * nch * 2;
        fseek(rec.f, 0, SEEK_SET);
        wav_header(rec.f, nch, (int)sr, databytes);
        fclose(rec.f);
        /* path goes last: it can contain spaces, so it must not be mid-line */
        fprintf(stderr, "DONE frames=%llu secs=%.3f rate=%.0f ch=%d file=%s\n",
                rec.frames, rec.frames / sr, sr, nch, outpath);
    } else {
        fprintf(stderr, "captured %llu frames (nothing written)\n", rec.frames);
    }
    if (rec.frames == 0) {
        fprintf(stderr, "NO AUDIO RECEIVED - check System Settings > Privacy & Security > Microphone\n");
        return 3;
    }
    return 0;
}
