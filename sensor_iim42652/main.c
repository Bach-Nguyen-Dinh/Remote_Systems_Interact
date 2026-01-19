#define _GNU_SOURCE
#include <stdio.h>
#include <unistd.h>
#include <signal.h>
#include "iim42652.h"

volatile sig_atomic_t flag = 0;

void ctrl_c_event(int sig)
{
    (void)sig;
    write(STDOUT_FILENO, "\nCTRL+C received. Force exit.\n", 30);
    _exit(1);   // immediate termination (like kill -9)
}

int main(void)
{
	IIM42652_axis_t accel_data;
	IIM42652_axis_t gyro_data;
	// float temp;

	float acc_x, acc_y, acc_z;
	float gyro_x, gyro_y, gyro_z;

	struct sigaction sa;
	sa.sa_handler = ctrl_c_event;
	sigemptyset(&sa.sa_mask);
	sa.sa_flags = 0;
	sigaction(SIGINT, &sa, NULL);

	//Open sensor
	iim42652_open();

	//Check sensor availability
	if (!iim42652_avail()) {
		// printf("Sensor found!\n");
	} else {
		printf("Sensor not found check your connections!\n");
		goto err;	
	}

	// while (1) {
		iim42652_ex_idle();
		iim42652_accelerometer_enable();
		iim42652_gyroscope_enable();
		// iim42652_temperature_enable();

		usleep(100 * 1000);

		iim42652_get_accel_data(&accel_data);
		iim42652_get_gyro_data(&gyro_data);
		// iim42652_get_temperature(&temp);

		/*
		 * ±16 g  : 2048  LSB/g
		 * ±8 g   : 4096  LSB/g
		 * ±4 g   : 8192  LSB/g
		 * ±2 g   : 16384 LSB/g
		*/
		acc_x = (float)accel_data.x / 2048;
		acc_y = (float)accel_data.y / 2048;
		acc_z = (float)accel_data.z / 2048;


		printf("accel_x: %f\n", acc_x);
		printf("accel_y: %f\n", acc_y);
		printf("accel_z: %f\n", acc_z);

		/*
		* ±2000 º/s    : 16.4   LSB/(º/s)
		* ±1000 º/s    : 32.8   LSB/(º/s)
		* ±500  º/s    : 65.5   LSB/(º/s)
		* ±250  º/s    : 131    LSB/(º/s)
		* ±125  º/s    : 262    LSB/(º/s)
		* ±62.5  º/s   : 524.3  LSB/(º/s)
		* ±31.25  º/s  : 1048.6 LSB/(º/s)
		* ±15.625 º/s  : 2097.2 LSB/(º/s)
		*/
		gyro_x = (float)gyro_data.x / 16.4;
		gyro_y = (float)gyro_data.y / 16.4;
		gyro_z = (float)gyro_data.z / 16.4;

		printf("gyro_x: %f\n", gyro_x);
		printf("gyro_y: %f\n", gyro_y);
		printf("gyro_z: %f\n", gyro_z);

		// printf("Temperature : %f[ºC]\n", temp);

		iim42652_accelerometer_disable();
		iim42652_gyroscope_disable();
		// iim42652_temperature_disable();
		iim42652_idle();
		// usleep(2000 * 1000);

	// 	if (flag)
	// 		break;
	// }

err:
	//Close sensor
	iim42652_close();
	return 0;
}
