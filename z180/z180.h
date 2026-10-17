/* z180.h - Z180 helpers for uc80 programs (implemented in z180.mac).
   Build with   cc.ps1 prog -Cpu z180   or add   --embed-lib z180/z180.mac   to uc80. */
#ifndef Z180_H
#define Z180_H

unsigned char z180_in0(unsigned char port);              /* IN0  A,(port)  internal I/O */
void z180_out0(unsigned char port, unsigned char value);   /* OUT0 (port),A */
unsigned int z180_mlt(unsigned char a, unsigned char b);   /* a * b with MLT */

/* MMU registers.  Logical 4K pages below BA are common area 0 (physical = logical),
   pages from BA to CA-1 the bank area (physical = logical + BBR * 4K), pages from CA up
   common area 1 (physical = logical + CBR * 4K).  CBAR holds CA in its high nibble and
   BA in its low one; after reset CBAR = F0h, BBR = CBR = 0: the plain 64K. */
#define Z180_CBR  0x38
#define Z180_BBR  0x39
#define Z180_CBAR 0x3A

/* Make the 16K window at 8000h-BFFFh show physical memory from `phys` (rounded down to
   4K) and return a pointer to `phys` in it.  The program's code and data must stay below
   8000h; C000h-FFFFh (the stack, BDOS) is left as it is.  Physical memory is 1 MB (00000h-
   FFFFFh); the first 64K is the CP/M memory itself, so use 10000h and up. */
unsigned char *z180_window(unsigned long phys);
void z180_unwindow(void);                                  /* back to the plain 64K */

#endif
