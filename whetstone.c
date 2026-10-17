/* Whetstone benchmark (Curnow & Wichmann 1976), classic module mix.
   ITER 1 = 100,000 Whetstone instructions; KWIPS = 100 * ITER / seconds. */
#include <stdio.h>
#include <math.h>

#ifndef ITER
#define ITER 1
#endif

double t, t1, t2;
double e1[5];
int j, k, l;

void pa(double *e)
{
    int n = 0;
    do {
        e[1] = (e[1] + e[2] + e[3] - e[4]) * t;
        e[2] = (e[1] + e[2] - e[3] + e[4]) * t;
        e[3] = (e[1] - e[2] + e[3] + e[4]) * t;
        e[4] = (-e[1] + e[2] + e[3] + e[4]) / t2;
        n++;
    } while (n < 6);
}

void p3(double x, double y, double *z)
{
    double x1, y1;
    x1 = t * (x + y);
    y1 = t * (x1 + y);
    *z = (x1 + y1) / t2;
}

void p0(void)
{
    e1[j] = e1[k];
    e1[k] = e1[l];
    e1[l] = e1[j];
}

int main(void)
{
    int i, n2, n3, n4, n6, n7, n8, n9, n11;
    double x, y, z;

    t = 0.499975; t1 = 0.50025; t2 = 2.0;
    n2 = 12 * ITER; n3 = 14 * ITER; n4 = 345 * ITER; n6 = 210 * ITER;
    n7 = 32 * ITER; n8 = 899 * ITER; n9 = 616 * ITER; n11 = 93 * ITER;

    e1[1] = 1.0; e1[2] = -1.0; e1[3] = -1.0; e1[4] = -1.0;
    for (i = 0; i < n2; i++) {
        e1[1] = (e1[1] + e1[2] + e1[3] - e1[4]) * t;
        e1[2] = (e1[1] + e1[2] - e1[3] + e1[4]) * t;
        e1[3] = (e1[1] - e1[2] + e1[3] + e1[4]) * t;
        e1[4] = (-e1[1] + e1[2] + e1[3] + e1[4]) * t;
    }
    printf("M2  %d %f %f\r\n", n2, e1[1], e1[4]);

    for (i = 0; i < n3; i++)
        pa(e1);
    printf("M3  %d %f %f\r\n", n3, e1[1], e1[4]);

    j = 1;
    for (i = 0; i < n4; i++) {
        if (j == 1) j = 2; else j = 3;
        if (j > 2) j = 0; else j = 1;
        if (j < 1) j = 1; else j = 0;
    }
    printf("M4  %d %d\r\n", n4, j);

    j = 1; k = 2; l = 3;
    for (i = 0; i < n6; i++) {
        j = j * (k - j) * (l - k);
        k = l * k - (l - j) * k;
        l = (l - k) * (k + j);
        e1[l - 1] = j + k + l;
        e1[k - 1] = j * k * l;
    }
    printf("M6  %d %d %d %d\r\n", n6, j, k, l);

    x = 0.5; y = 0.5;
    for (i = 0; i < n7; i++) {
        x = t * atan(t2 * sin(x) * cos(x) / (cos(x + y) + cos(x - y) - 1.0));
        y = t * atan(t2 * sin(y) * cos(y) / (cos(x + y) + cos(x - y) - 1.0));
    }
    printf("M7  %d %f %f\r\n", n7, x, y);

    x = 1.0; y = 1.0; z = 1.0;
    for (i = 0; i < n8; i++)
        p3(x, y, &z);
    printf("M8  %d %f\r\n", n8, z);

    j = 1; k = 2; l = 3;
    e1[1] = 1.0; e1[2] = 2.0; e1[3] = 3.0;
    for (i = 0; i < n9; i++)
        p0();
    printf("M9  %d %f %f\r\n", n9, e1[1], e1[3]);

    x = 0.75;
    for (i = 0; i < n11; i++)
        x = sqrt(exp(log(x) / t1));
    printf("M11 %d %f\r\n", n11, x);
    return 0;
}
