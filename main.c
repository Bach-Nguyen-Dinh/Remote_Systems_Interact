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
    _exit(1);
}

// Get current time in seconds (wall clock)
static double get_time_seconds(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

int main() {
	IIM42652_axis_t accel_data;
	IIM42652_axis_t gyro_data;

	float acc_x, acc_y, acc_z;
	float gyro_x, gyro_y, gyro_z;

	struct sigaction sa;
	sa.sa_handler = ctrl_c_event;
	sigemptyset(&sa.sa_mask);
	sa.sa_flags = 0;
	sigaction(SIGINT, &sa, NULL);

	iim42652_open();

	if (iim42652_avail()) {
		printf("Sensor not found check your connections!\n");
		goto err;	
	}

    // ========== DYNAMIC CALIBRATION PHASE ==========
    printf("=== Calibration Phase ===\n");
    printf("Keep the sensor STILL for 10 seconds...\n");

    float gyro_sum_x = 0.0f, gyro_sum_y = 0.0f, gyro_sum_z = 0.0f;
    float accel_sum_x = 0.0f, accel_sum_y = 0.0f, accel_sum_z = 0.0f;

    for (int i = 0; i < CALIBRATION_SAMPLES; i++) {
        iim42652_ex_idle();
        iim42652_accelerometer_enable();
        iim42652_gyroscope_enable();

        usleep(SAMPLE_PERIOD_US);

        iim42652_get_accel_data(&accel_data);
        iim42652_get_gyro_data(&gyro_data);

        iim42652_accelerometer_disable();
        iim42652_gyroscope_disable();
        iim42652_idle();

        // Convert to physical units
        acc_x = (float)accel_data.x / 2048.0f;
        acc_y = (float)accel_data.y / 2048.0f;
        acc_z = (float)accel_data.z / 2048.0f;

        gyro_x = (float)gyro_data.x / 16.4f;
        gyro_y = (float)gyro_data.y / 16.4f;
        gyro_z = (float)gyro_data.z / 16.4f;

        gyro_sum_x += gyro_x;
        gyro_sum_y += gyro_y;
        gyro_sum_z += gyro_z;

        accel_sum_x += acc_x;
        accel_sum_y += acc_y;
        accel_sum_z += acc_z;

        // Progress indicator
        if ((i + 1) % 10 == 0) {
            printf("Calibrating... %d%%\n", (i + 1) * 100 / CALIBRATION_SAMPLES);
        }
    }

    // Calculate gyroscope offset (average bias at rest)
    FusionVector gyroscopeOffset = {
        {gyro_sum_x / CALIBRATION_SAMPLES,
         gyro_sum_y / CALIBRATION_SAMPLES,
         gyro_sum_z / CALIBRATION_SAMPLES}
    };

    // Calculate accelerometer offset (should be ~0, 0, -1g when flat)
    // We only correct X and Y, Z should read -1g (or +1g depending on orientation)
    FusionVector accelerometerOffset = {
        {accel_sum_x / CALIBRATION_SAMPLES,
         accel_sum_y / CALIBRATION_SAMPLES,
         0.0f}  // Don't offset Z - gravity is real
    };

    printf("=== Calibration Complete ===\n");
    printf("Gyro offset: X=%.3f, Y=%.3f, Z=%.3f deg/s\n",
           gyroscopeOffset.axis.x, gyroscopeOffset.axis.y, gyroscopeOffset.axis.z);
    printf("Accel offset: X=%.4f, Y=%.4f g\n",
           accelerometerOffset.axis.x, accelerometerOffset.axis.y);
    printf("============================\n\n");

    // Identity matrices (no misalignment correction)
    const FusionMatrix gyroscopeMisalignment = {
        {1.0f, 0.0f, 0.0f, 
        0.0f, 1.0f, 0.0f, 
        0.0f, 0.0f, 1.0f}
    };
    const FusionVector gyroscopeSensitivity = {{1.0f, 1.0f, 1.0f}};

    const FusionMatrix accelerometerMisalignment = {
        {1.0f, 0.0f, 0.0f, 
        0.0f, 1.0f, 0.0f, 
        0.0f, 0.0f, 1.0f}
    };
    const FusionVector accelerometerSensitivity = {{1.0f, 1.0f, 1.0f}};

    // Initialize AHRS (no FusionBias - we handle calibration ourselves)
    FusionAhrs ahrs;
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

    // Timing
    double previousTime = get_time_seconds();

    // Main loop
    while (true) {
		iim42652_ex_idle();
		iim42652_accelerometer_enable();
		iim42652_gyroscope_enable();

		usleep(100 * 1000);

		iim42652_get_accel_data(&accel_data);
		iim42652_get_gyro_data(&gyro_data);

		iim42652_accelerometer_disable();
		iim42652_gyroscope_disable();
		iim42652_idle();

        // Convert to physical units
        acc_x = (float)accel_data.x / 2048.0f;
        acc_y = (float)accel_data.y / 2048.0f;
        acc_z = (float)accel_data.z / 2048.0f;

        gyro_x = (float)gyro_data.x / 16.4f;
        gyro_y = (float)gyro_data.y / 16.4f;
        gyro_z = (float)gyro_data.z / 16.4f;

        FusionVector gyroscope = {{gyro_x, gyro_y, gyro_z}};
        FusionVector accelerometer = {{acc_x, acc_y, acc_z}};

        // Apply calibration offsets
        gyroscope = FusionModelInertial(gyroscope, gyroscopeMisalignment,
                                        gyroscopeSensitivity, gyroscopeOffset);
        accelerometer = FusionModelInertial(accelerometer, accelerometerMisalignment,
                                            accelerometerSensitivity, accelerometerOffset);

        // Calculate delta time using wall clock
        double currentTime = get_time_seconds();
        float deltaTime = (float)(currentTime - previousTime);
        previousTime = currentTime;

        // Sanity check deltaTime
        if (deltaTime <= 0.0f || deltaTime > 1.0f) {
            deltaTime = 1.0f / FUSION_SAMPLE_RATE;
        }

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

