
/*
 * DeepSeekHashV1 proof-of-work solver.
 *
 * The web client uses a SHA3-256 variant whose Keccak-f[1600] permutation
 * skips round 0 (rounds 1..23 only). The server picks a nonce in
 * [0, difficulty) and publishes:
 *
 *     challenge = DeepSeekHashV1(salt + "_" + expire_at + "_" + nonce)
 *
 * Build:  gcc -O3 -o ds_pow_solver ds_pow_solver.c
 * Usage:  ds_pow_solver solve <challenge_hex> <salt> <expire_at> <difficulty>
 * Output: the nonce on stdout, or -1 if none was found.
 */
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

static const uint64_t RC[24] = {
    0x0000000000000001ULL, 0x0000000000008082ULL, 0x800000000000808AULL, 0x8000000080008000ULL,
    0x000000000000808BULL, 0x0000000080000001ULL, 0x8000000080008081ULL, 0x8000000000008009ULL,
    0x000000000000008AULL, 0x0000000000000088ULL, 0x0000000080008009ULL, 0x000000008000000AULL,
    0x000000008000808BULL, 0x800000000000008BULL, 0x8000000000008089ULL, 0x8000000000008003ULL,
    0x8000000000008002ULL, 0x8000000000000080ULL, 0x000000000000800AULL, 0x800000008000000AULL,
    0x8000000080008081ULL, 0x8000000000008080ULL, 0x0000000080000001ULL, 0x8000000080008008ULL
};

static inline uint64_t rotl(uint64_t v, unsigned k) {
    return k ? ((v << k) | (v >> (64 - k))) : v;
}

#define KECCAK_ROUND(s0,s1,s2,s3,s4,s5,s6,s7,s8,s9,s10,s11,s12,s13,s14,s15,s16,s17,s18,s19,s20,s21,s22,s23,s24, r) do { \
    uint64_t c0 = s0 ^ s5 ^ s10 ^ s15 ^ s20; \
    uint64_t c1 = s1 ^ s6 ^ s11 ^ s16 ^ s21; \
    uint64_t c2 = s2 ^ s7 ^ s12 ^ s17 ^ s22; \
    uint64_t c3 = s3 ^ s8 ^ s13 ^ s18 ^ s23; \
    uint64_t c4 = s4 ^ s9 ^ s14 ^ s19 ^ s24; \
    uint64_t d0 = c4 ^ rotl(c1, 1); \
    uint64_t d1 = c0 ^ rotl(c2, 1); \
    uint64_t d2 = c1 ^ rotl(c3, 1); \
    uint64_t d3 = c2 ^ rotl(c4, 1); \
    uint64_t d4 = c3 ^ rotl(c0, 1); \
    s0 ^= d0; s5 ^= d0; s10 ^= d0; s15 ^= d0; s20 ^= d0; \
    s1 ^= d1; s6 ^= d1; s11 ^= d1; s16 ^= d1; s21 ^= d1; \
    s2 ^= d2; s7 ^= d2; s12 ^= d2; s17 ^= d2; s22 ^= d2; \
    s3 ^= d3; s8 ^= d3; s13 ^= d3; s18 ^= d3; s23 ^= d3; \
    s4 ^= d4; s9 ^= d4; s14 ^= d4; s19 ^= d4; s24 ^= d4; \
    uint64_t b0 = s0; \
    uint64_t b10 = rotl(s1, 1); \
    uint64_t b20 = rotl(s2, 62); \
    uint64_t b5  = rotl(s3, 28); \
    uint64_t b15 = rotl(s4, 27); \
    uint64_t b16 = rotl(s5, 36); \
    uint64_t b1  = rotl(s6, 44); \
    uint64_t b11 = rotl(s7, 6); \
    uint64_t b21 = rotl(s8, 55); \
    uint64_t b6  = rotl(s9, 20); \
    uint64_t b7  = rotl(s10, 3); \
    uint64_t b17 = rotl(s11, 10); \
    uint64_t b2  = rotl(s12, 43); \
    uint64_t b12 = rotl(s13, 25); \
    uint64_t b22 = rotl(s14, 39); \
    uint64_t b23 = rotl(s15, 41); \
    uint64_t b8  = rotl(s16, 45); \
    uint64_t b18 = rotl(s17, 15); \
    uint64_t b3  = rotl(s18, 21); \
    uint64_t b13 = rotl(s19, 8); \
    uint64_t b14 = rotl(s20, 18); \
    uint64_t b24 = rotl(s21, 2); \
    uint64_t b9  = rotl(s22, 61); \
    uint64_t b19 = rotl(s23, 56); \
    uint64_t b4  = rotl(s24, 14); \
    s0  = b0  ^ (~b1  & b2); \
    s1  = b1  ^ (~b2  & b3); \
    s2  = b2  ^ (~b3  & b4); \
    s3  = b3  ^ (~b4  & b0); \
    s4  = b4  ^ (~b0  & b1); \
    s5  = b5  ^ (~b6  & b7); \
    s6  = b6  ^ (~b7  & b8); \
    s7  = b7  ^ (~b8  & b9); \
    s8  = b8  ^ (~b9  & b5); \
    s9  = b9  ^ (~b5  & b6); \
    s10 = b10 ^ (~b11 & b12); \
    s11 = b11 ^ (~b12 & b13); \
    s12 = b12 ^ (~b13 & b14); \
    s13 = b13 ^ (~b14 & b10); \
    s14 = b14 ^ (~b10 & b11); \
    s15 = b15 ^ (~b16 & b17); \
    s16 = b16 ^ (~b17 & b18); \
    s17 = b17 ^ (~b18 & b19); \
    s18 = b18 ^ (~b19 & b15); \
    s19 = b19 ^ (~b15 & b16); \
    s20 = b20 ^ (~b21 & b22); \
    s21 = b21 ^ (~b22 & b23); \
    s22 = b22 ^ (~b23 & b24); \
    s23 = b23 ^ (~b24 & b20); \
    s24 = b24 ^ (~b20 & b21); \
    s0 ^= RC[r]; \
} while (0)

static void ds_hash(const uint8_t *data, size_t len, uint8_t out[32]) {
    uint64_t s0=0,s1=0,s2=0,s3=0,s4=0,s5=0,s6=0,s7=0,s8=0,s9=0,s10=0,s11=0,s12=0,s13=0,s14=0,s15=0,s16=0,s17=0,s18=0,s19=0,s20=0,s21=0,s22=0,s23=0,s24=0;
    const size_t rate = 136;
    size_t off = 0;
    uint64_t w[17];
    while (off + rate <= len) {
        memcpy(w, data + off, rate);
        s0^=w[0]; s1^=w[1]; s2^=w[2]; s3^=w[3]; s4^=w[4]; s5^=w[5]; s6^=w[6]; s7^=w[7]; s8^=w[8];
        s9^=w[9]; s10^=w[10]; s11^=w[11]; s12^=w[12]; s13^=w[13]; s14^=w[14]; s15^=w[15]; s16^=w[16];
        for (int r = 1; r < 24; r++) KECCAK_ROUND(s0,s1,s2,s3,s4,s5,s6,s7,s8,s9,s10,s11,s12,s13,s14,s15,s16,s17,s18,s19,s20,s21,s22,s23,s24,r);
        off += rate;
    }
    uint8_t buf[136];
    memset(buf, 0, sizeof(buf));
    size_t rem = len - off;
    memcpy(buf, data + off, rem);
    buf[rem] = 0x06;
    buf[135] |= 0x80;
    memcpy(w, buf, rate);
    s0^=w[0]; s1^=w[1]; s2^=w[2]; s3^=w[3]; s4^=w[4]; s5^=w[5]; s6^=w[6]; s7^=w[7]; s8^=w[8];
    s9^=w[9]; s10^=w[10]; s11^=w[11]; s12^=w[12]; s13^=w[13]; s14^=w[14]; s15^=w[15]; s16^=w[16];
    for (int r = 1; r < 24; r++) KECCAK_ROUND(s0,s1,s2,s3,s4,s5,s6,s7,s8,s9,s10,s11,s12,s13,s14,s15,s16,s17,s18,s19,s20,s21,s22,s23,s24,r);
    w[0]=s0; w[1]=s1; w[2]=s2; w[3]=s3;
    memcpy(out, w, 32);
}

static int hex_to_bytes(const char *hex, uint8_t *out, int max) {
    int n = 0;
    while (hex[0] && hex[1] && n < max) {
        unsigned v;
        if (sscanf(hex, "%2x", &v) != 1) return -1;
        out[n++] = (uint8_t)v;
        hex += 2;
    }
    return n;
}

int main(int argc, char **argv) {
    if (argc != 6 || strcmp(argv[1], "solve") != 0) {
        fprintf(stderr, "usage: %s solve <challenge_hex> <salt> <expire_at> <difficulty>\n", argv[0]);
        return 2;
    }
    uint8_t target[32];
    if (hex_to_bytes(argv[2], target, 32) != 32) {
        fprintf(stderr, "invalid challenge\n");
        return 2;
    }
    const char *salt = argv[3];
    long long expire = strtoll(argv[4], NULL, 10);
    long long difficulty = strtoll(argv[5], NULL, 10);
    if (difficulty <= 0) {
        fprintf(stderr, "invalid difficulty\n");
        return 2;
    }
    uint8_t input[256];
    uint8_t h[32];
    int plen = snprintf((char *)input, sizeof(input), "%s_%lld_", salt, expire);
    if (plen <= 0 || plen >= 200) {
        fprintf(stderr, "prefix too long\n");
        return 2;
    }
    for (long long n = 0; n < difficulty; n++) {
        int m = snprintf((char *)input + plen, sizeof(input) - (size_t)plen, "%lld", n);
        ds_hash(input, (size_t)(plen + m), h);
        if (memcmp(h, target, 32) == 0) {
            printf("%lld\n", n);
            return 0;
        }
    }
    printf("-1\n");
    return 1;
}
