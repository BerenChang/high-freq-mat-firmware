/*
main.c file for V2 of the RPi Pico of the PMI
*/

#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>

#include "pico/stdlib.h"
#include "hardware/spi.h"
#include "hardware/gpio.h"
#include "pico/util/queue.h"

#include "transmitter.h"
#include "adc.h"
#include "matterface.h"
#include "custom_multicore.h"

#define LED_PIN             25
#define INPUT_BUFFER_SIZE   256
#define NO_COMMAND_AVAILABLE 0

// The queue that passes data from core0 to core1
queue_t queue;

/*
 * Read one newline-terminated command from USB serial.
 *
 * In blocking mode this waits for a complete command. In non-blocking mode it
 * consumes all currently available characters and returns
 * NO_COMMAND_AVAILABLE when no complete line has arrived yet. input_pointer is
 * retained between calls so commands can arrive in several USB packets.
 */
static int receive_command(char *input_string,
                           size_t *input_pointer,
                           bool blocking)
{
    while (true) {
        int ch = blocking ? getchar() : getchar_timeout_us(0);

        if (ch == PICO_ERROR_TIMEOUT) {
            if (blocking) {
                continue;
            }
            return NO_COMMAND_AVAILABLE;
        }

        if ((ch == '\n') || (ch == '\r')) {
            // Ignore the second character in a CRLF pair and other blank lines.
            if (*input_pointer == 0) {
                continue;
            }

            input_string[*input_pointer] = '\0';
            *input_pointer = 0;
            return parse_input(input_string);
        }

        if (*input_pointer < (INPUT_BUFFER_SIZE - 1)) {
            input_string[*input_pointer] = (char)ch;
            (*input_pointer)++;
        }
        // If the line is too long, keep the truncated command and reject it
        // when its newline arrives rather than writing beyond the buffer.
    }
}

int core1_main()
{
    /*
    Core 1 handles transmitting the mat data to the connected computer in parallel with mat reads.
    As data is received on the queue, send it over serial sequentially. Once a full mat read has
    been sent, send the verification sequence.
    */

    int i;
    struct queueItem temp;

    while (1) {
        for (i = 0; i < COL_HEIGHT; i++) {
            queue_remove_blocking(&queue, &temp);
            transmit_row(temp.row_data);
        }

        transmit_verification();
    }

    return 1;   // should never exit
}

int main()
{
    initialize_transmitter();

    // Delay with some LED blinking on startup
    gpio_init(LED_PIN);
    gpio_set_dir(LED_PIN, GPIO_OUT);
    for (int i = 0; i < 5; i++) {
        sleep_ms(100);
        gpio_put(LED_PIN, 0);
        sleep_ms(100);
        gpio_put(LED_PIN, 1);
    }

    // initialize the adcs
    struct adc_inst *adc1 = malloc(sizeof(struct adc_inst));
    struct adc_inst *adc2 = malloc(sizeof(struct adc_inst));
    initialize_adcs(adc1, adc2, true);  // true for dual_channel mode

    // initialize the shift registers
    initialize_shreg_pins();

    // initialize the matterface End Of Conversion interrupts
    initialize_EOC_interrupts();

    // initialize the queue for passing data between the two cores
    queue_init(&queue, sizeof(item), COL_HEIGHT * 2);

    // start up the second core
    launch_core1((void *)core1_main);

    char input_string[INPUT_BUFFER_SIZE];
    size_t input_pointer = 0;
    bool continuous_reading = false;

    while (1) {
        if (continuous_reading) {
            /*
             * Finish one complete frame before checking for stop_reading. This
             * guarantees that core 1 never receives a partial 56-row frame and
             * therefore never blocks halfway through a transmission.
             */
            read_mat(&queue, adc1, adc2);

            int parsed_command;
            while ((parsed_command = receive_command(input_string,
                                                       &input_pointer,
                                                       false))
                   != NO_COMMAND_AVAILABLE) {
                if (parsed_command == STOP_READING_COMMAND_ID) {
                    continuous_reading = false;
                    clear_shreg();
                    gpio_put(LED_PIN, 1);
                    break;
                }

                // Other commands are intentionally ignored while binary mat
                // data is being streamed so text cannot corrupt a frame.
            }

            continue;
        }

        // When stopped, wait for the next complete command from the host.
        int parsed_command = receive_command(input_string, &input_pointer, true);

        if (parsed_command == START_READING_COMMAND_ID) {
            // Clear row selection; read_mat() reinitializes all per-frame ADC state.
            clear_shreg();
            continuous_reading = true;
            gpio_put(LED_PIN, 0);
        }
        else if (parsed_command == STOP_READING_COMMAND_ID) {
            // stop_reading is idempotent when the board is already stopped.
            clear_shreg();
            gpio_put(LED_PIN, 1);
        }
        else if (parsed_command == GET_CAL_VALS_COMMAND_ID) {
            // perform one read of the mat and transmit it to the GUI
            clear_shreg();
            read_mat(&queue, adc1, adc2);
        }
        else if (parsed_command == PRINT_INFO_COMMAND_ID) {
            // print code info
            printf("This is PressureMat software for board v2!\n");
        }
        else {
            printf("Unrecognized command\n");
        }
    }

    return 1;   // should never exit
}