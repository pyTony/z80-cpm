/* banks.c - Z180 example: keep data in memory above 64K and multiply with MLT.
   cc.ps1 banks -Cpu z180   (needs z180.h and z180.mac from this folder) */
#include <stdio.h>
#include "z180.h"

int main(void)
{
    unsigned char *p;
    unsigned long base;
    unsigned int i, sum, bank;

    for (bank = 1; bank < 16; bank++) {          /* 4K of data in each 64K of physical memory */
        base = (unsigned long)bank << 16;
        p = z180_window(base);
        for (i = 0; i < 4096; i++)
            p[i] = (unsigned char)bank;
    }
    for (bank = 1; bank < 16; bank++) {
        p = z180_window((unsigned long)bank << 16);
        sum = 0;
        for (i = 0; i < 4096; i++)
            sum += p[i];
        printf("bank %2u at %05lXh: sum %u (expected %u)\r\n", bank, (unsigned long)bank << 16, sum, bank * 4096u);
    }
    z180_unwindow();
    printf("CBAR=%02X BBR=%02X  200*200=%u\r\n", z180_in0(Z180_CBAR), z180_in0(Z180_BBR),
           z180_mlt(200, 200));
    return 0;
}
