/*
C file responsible for sending data over serial usb to the computer
*/

#include <stdbool.h>
#include <string.h>

#include "hardware/gpio.h"

#include "matterface.h"
#include "transmitter.h"

int parse_input(char *string)
{
    // Use exact comparisons. The old sizeof(string) expression only compared
    // the first 4/8 bytes because string is a pointer.
    if (strcmp(string, START_READING_COMMAND) == 0) {
        return START_READING_COMMAND_ID;
    }
    else if (strcmp(string, STOP_READING_COMMAND) == 0) {
        return STOP_READING_COMMAND_ID;
    }
    else if (strcmp(string, GET_CAL_VALS_COMMAND) == 0) {
        return GET_CAL_VALS_COMMAND_ID;
    }
    else if (strcmp(string, PRINT_INFO_COMMAND) == 0) {
        return PRINT_INFO_COMMAND_ID;
    }
    else {
        return -1;  // no valid command read
    }
}

void initialize_transmitter()
{
    // enable UART printfs
    stdio_init_all();
}

void transmit_verification()
{
    // transmit {255, 254, 254, 255} because maximum real-world mat value is ~207
    putchar_raw(255);
    putchar_raw(254);
    putchar_raw(254);
    putchar_raw(255);

    fflush(stdout);
}

void transmit_row(uint8_t *row)
{
    // transmits one row over serial in hexadecimal format
    int i;
    for (i = 0; i < ROW_WIDTH; i++) {
        putchar_raw(row[i]);
    }

    // does not close with newline: to finish transmission, send the verification sequence
}

void transmit_mat(uint8_t *mat)
{
    sleep_ms(THROTTLE_SLEEP_MS);    // THROTTLE_SLEEP_MS set by CMake
    int i;
    for (i = 0; i < COL_HEIGHT; i++) {
        transmit_row(mat + (i * ROW_WIDTH));
    }
    transmit_verification();
}