#define _GNU_SOURCE
#include "main.h"

#define FUSION_SAMPLE_RATE (100) // Hz (adjust based on actual IMU sample rate ~300ms = ~3.3Hz)
#define FUSION_GYRO_RANGE 2000.0  // deg/s
#define FUSION_GAIN 0.5  // AHRS algorithm gain
#define FUSION_ACCEL_REJECTION 10.0  // Acceleration rejection threshold

volatile sig_atomic_t flag = 0;

void ctrl_c_event(int sig)
{
    (void)sig;
    write(STDOUT_FILENO, "\nCTRL+C received. Force exit.\n", 30);
    _exit(1);   // immediate termination (like kill -9)
}

int main() {
    // Initialize sensor
	IIM42652_axis_t accel_data;
	IIM42652_axis_t gyro_data;

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
	if (iim42652_avail()) {
		printf("Sensor not found check your connections!\n");
		goto err;	
	}

    // Calibration parameters (replace with actual calibration data)
    const FusionMatrix gyroscopeMisalignment = {
        {1.0f, 0.0f, 0.0f, 
        0.0f, 1.0f, 0.0f, 
        0.0f, 0.0f, 1.0f}
    };
    const FusionVector gyroscopeSensitivity = {
        {1.0f, 1.0f, 1.0f}
    };
    // Measured gyroscope bias at rest (calibrate for your sensor)
    const FusionVector gyroscopeOffset = {
        {0.8f, -1.8f, 0.3f}
    };

    const FusionMatrix accelerometerMisalignment = {
        {1.0f, 0.0f, 0.0f, 
        0.0f, 1.0f, 0.0f, 
        0.0f, 0.0f, 1.0f}
    };
    const FusionVector accelerometerSensitivity = {
        {1.0f, 1.0f, 1.0f}
    };
    const FusionVector accelerometerOffset = {
        {0.0f, 0.0f, 0.0f}
    };

    // Initialise structures
    FusionBias bias;
    FusionAhrs ahrs;

    FusionBiasInitialise(&bias, FUSION_SAMPLE_RATE);
    FusionAhrsInitialise(&ahrs);

    // Set AHRS settings
    const FusionAhrsSettings settings = {
        .convention = FusionConventionNwu,
        .gain = FUSION_GAIN,
        .gyroscopeRange = FUSION_GYRO_RANGE,
        .accelerationRejection = FUSION_ACCEL_REJECTION,
        .magneticRejection = 10.0f,
        .recoveryTriggerPeriod = 5 * FUSION_SAMPLE_RATE, /* 5 seconds */
    };

    FusionAhrsSetSettings(&ahrs, &settings);

    // This loop should repeat for each new gyroscope measurement
    while (true) {
        // Read sensors
		iim42652_ex_idle();
		iim42652_accelerometer_enable();
		iim42652_gyroscope_enable();

		usleep(100 * 1000);

		iim42652_get_accel_data(&accel_data);
		iim42652_get_gyro_data(&gyro_data);

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

        const clock_t timestamp = clock();
        FusionVector gyroscope = {
            {gyro_x, gyro_y, gyro_z}
        };
        FusionVector accelerometer = {
            {acc_x, acc_y, acc_z}
        };

		iim42652_accelerometer_disable();
		iim42652_gyroscope_disable();
		iim42652_idle();

        // Apply calibration
        gyroscope = FusionModelInertial(gyroscope, gyroscopeMisalignment, gyroscopeSensitivity, gyroscopeOffset);

        accelerometer = FusionModelInertial(accelerometer, accelerometerMisalignment, accelerometerSensitivity, accelerometerOffset);

        // Update bias algorithm
        gyroscope = FusionBiasUpdate(&bias, gyroscope);

        // Calculate delta time to compensate for gyroscope sample clock errors
        static clock_t previousTimestamp = 0;
        static int firstIteration = 1;
        float deltaTime;
        if (firstIteration) {
            deltaTime = 1.0f / FUSION_SAMPLE_RATE;  // Use nominal sample period for first iteration
            firstIteration = 0;
        } else {
            deltaTime = (float) (timestamp - previousTimestamp) / (float) CLOCKS_PER_SEC;
        }
        previousTimestamp = timestamp;

        // Update AHRS algorithm
        FusionAhrsUpdateNoMagnetometer(&ahrs, gyroscope, accelerometer, deltaTime);

        // Print AHRS outputs
        const FusionEuler euler = FusionQuaternionToEuler(FusionAhrsGetQuaternion(&ahrs));

        const FusionVector earth = FusionAhrsGetEarthAcceleration(&ahrs);

        printf("Roll %0.1f, Pitch %0.1f, Yaw %0.1f, X %0.1f, Y %0.1f, Z %0.1f\n",
               euler.angle.roll, euler.angle.pitch, euler.angle.yaw,
               earth.axis.x, earth.axis.y, earth.axis.z);
    }

    err:
        //Close sensor
        iim42652_close();
        return 0;
}

