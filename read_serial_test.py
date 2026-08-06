import serial
import numpy as np
import threading

# Serial Configuration
SERIAL_PORT = "/dev/ttyACM0"
BAUD_RATE = 115200
START_READING_COMMAND = "start_reading"
STOP_READING_COMMAND = "stop_reading"
GET_CAL_VALS_COMMAND  = "get_cal_vals"
PRINT_INFO_COMMAND    = "print_info"
VERIFICATION_WIDTH = 4
ROW_WIDTH = 28
MAT_SIZE = 1568
VERIFICATION_SEQUENCE = [255, 254, 254, 255]

print_count = 0

# Data Buffer: 9x9 Matrix (initially all zeros)
data_buffer = [255, 254, 254, 255]

ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=10)

def prettyprint_mat(mat_as_list: list):
    """
    Python version of the board_code prettyprint_mat function. 
    Prints a list of length MAT_SIZE as a 2d matrix
    """

    line = ""
    for i in range(MAT_SIZE):
        if (i % ROW_WIDTH) == 0:
            print(line[:-1])
            line = ""
 
        line += f"{mat_as_list[i]:02x}, "


def mat_list_to_array_subsize(mat_as_list: list, width: int, height: int):
    """
    Converts a 1d python list (presumably the mat) to a 2D numpy array
    """

    array = np.empty((width, height), dtype=np.uint8)
    
    for i in range(height):
        array[:, i] = mat_as_list[i*ROW_WIDTH : i*ROW_WIDTH+width]

    return array


def read_serial():
    global print_count

    # with serial.Serial(port=SERIAL_PORT, baudrate=BAUD_RATE, timeout=10) as ser:
    # ser.set_buffer_size(rx_size = 1700, tx_size = 1700)

    # send the message to start reading the mat
    ser.write((START_READING_COMMAND + '\n').encode('utf-8'))
    # ser.write((GET_CAL_VALS_COMMAND + '\n').encode('utf-8'))
    polling = True

    # prev_sample_time_ns = time.time_ns()

    # continually poll serial for new mat data
    while polling:
        # mat data is transmitted as raw bytes``
        bytes = ser.read(VERIFICATION_WIDTH + MAT_SIZE)
        if bytes == b'':
            print("Serial timed out!")
            continue
        
        flat_mat = [x for x in bytes]
        # print(flat_mat)

        # ensure that the verifiation message was aligned
        for ver, val in zip(VERIFICATION_SEQUENCE, flat_mat[-4:]):
            if not (ver == val):
                # transmission_errors += 1
                print("====TRANSMISSION ERROR OCCURED! FIXING!!!====")
                # a verification error has occured, probably because the fifo filled up
                # to resolve it, simply wipe the fifo and read until the next verification sequence
                ser.reset_input_buffer()
                hist = np.zeros(VERIFICATION_WIDTH, dtype=np.uint8)

                # wait for verification sequence to be found, proving data stream is still intact
                while(not np.array_equal(hist, np.asarray(VERIFICATION_SEQUENCE, dtype=np.uint8))):
                    hist = np.roll(hist, -1)
                    hist[-1] = int.from_bytes(ser.read(1), "big")

                break   # do not false positive another error

        # prettyprint_mat(flat_mat)

        # process the collected data
        # data_array = mat_list_to_array(flat_mat)
        subarray = mat_list_to_array_subsize(flat_mat, 28, 56)
        # subarray = subarray.transpose()
        np.set_printoptions(threshold=np.inf, linewidth=np.inf)
        print("========================================================================")
        # print("[100k]----------------------------------------------------")
        # print(subarray[:3, :])
        # print("[50k]----------------------------------------------------")
        # print(subarray[3:8, :])
        # print("[10k]----------------------------------------------------")
        # print(subarray[8:13, :])
        # print("[1k]----------------------------------------------------")
        # print(subarray[13:18, :])
        # print("[510R]----------------------------------------------------")
        # print(subarray[18:23, :])
        # print("[100R]----------------------------------------------------")
        # print(subarray[23:28, :])
        # print(subarray[:10, :10])
        print(subarray)
        
        # if (print_count>10):
        #     prettyprint_mat(flat_mat)
        #     print_count = 0
        # else:
        #     print_count += 1
        data_buffer = subarray.tolist()

        print_count += 1

        if print_count > 100:
            ser.write((STOP_READING_COMMAND + '\n').encode('utf-8'))
            break


def read_serial_once():

    # with serial.Serial(port=SERIAL_PORT, baudrate=BAUD_RATE, timeout=10) as ser:
    # ser.set_buffer_size(rx_size = 1700, tx_size = 1700)

    # send the message to start reading the mat
    # ser.write((START_READING_COMMAND + '\n').encode('utf-8'))
    ser.write((GET_CAL_VALS_COMMAND + '\n').encode('utf-8'))

    # prev_sample_time_ns = time.time_ns()

    bytes = ser.read(VERIFICATION_WIDTH + MAT_SIZE)
    if bytes == b'':
        print("Serial timed out!")
        
    flat_mat = [x for x in bytes]
    # print(flat_mat)
    
    # prettyprint_mat(flat_mat)

    # process the collected data
    # data_array = mat_list_to_array(flat_mat)
    subarray = mat_list_to_array_subsize(flat_mat, 28, 56)
    np.set_printoptions(threshold=np.inf, linewidth=np.inf)
    print(subarray[:10, :10])


# read_serial_once()
read_serial()
