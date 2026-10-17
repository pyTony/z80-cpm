/* multest.c - checks the Z180 MLT multiplies (z180/mkruntime.py) against the uc80 originals:
   both builds must print the same.  Used by test_z80sim.py when uc80 is installed. */
#include <stdio.h>
unsigned int seed = 12345;
unsigned int rnd(void) { seed = seed * 25173 + 13849; return seed; }
int main(void)
{
    unsigned int i, a, b, s16 = 0;
    unsigned long la, lb, s32 = 0;
    long sa, sb, ss = 0;
    float f = 1.0, g;
    for (i = 0; i < 300; i++) {
        a = rnd(); b = rnd();
        s16 = s16 * 31 + a * b;
        la = ((unsigned long)a << 16) | rnd(); lb = ((unsigned long)rnd() << 8) ^ b;
        s32 = s32 * 7 + la * lb;
        sa = (long)la - 2000000000L; sb = -(long)(b >> 3);
        ss += sa * sb;
        g = (float)(a & 255) / 37.0 + 0.5;
        f = f * g;
        if (f > 1e20 || f < 1e-20) f = 1.0;
    }
    printf("%u %lu %ld %f\r\n", s16, s32, ss, f);
    printf("%u %lu\r\n", 65535u * 65535u, 100000UL * 300000UL);
    return 0;
}
