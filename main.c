#define _GNU_SOURCE
#include "main.h"
#include <stdlib.h>
#include <string.h>
#include <getopt.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>

// Default configuration (can be overridden via command line)
#define DEFAULT_SAMPLE_RATE_HZ      10      // Hz - single source of truth for timing
#define DEFAULT_CALIBRATION_SAMPLES 100     // Number of samples for calibration (~10s at 10Hz)
#define DEFAULT_UDP_PORT            8889    // UDP port to send data
#define DEFAULT_UDP_HOST            "127.0.0.1"

// Fusion algorithm parameters
#define FUSION_GYRO_RANGE           2000.0  // deg/s
#define FUSION_GAIN                 0.1     // Lower gain = trust accelerometer more
#define FUSION_ACCEL_REJECTION      10.0    // Acceleration rejection threshold

// Runtime configuration
typedef struct {
    int sample_rate_hz;
    int calibration_samples;
    char udp_host[64];
    int udp_port;
    int verbose;
} IMUConfig;

volatile sig_atomic_t running = 1;

void signal_handler(int sig)
{
    (void)sig;
    running = 0;
}

static void print_usage(const char *program_name)
{
    printf("Usage: %s [OPTIONS]\n", program_name);
    printf("\nOptions:\n");
    printf("  -r, --rate <Hz>       Sample rate in Hz (default: %d)\n", DEFAULT_SAMPLE_RATE_HZ);
    printf("  -c, --cal <samples>   Calibration samples (default: %d)\n", DEFAULT_CALIBRATION_SAMPLES);
    printf("  -H, --host <ip>       UDP destination host (default: %s)\n", DEFAULT_UDP_HOST);
    printf("  -p, --port <port>     UDP destination port (default: %d)\n", DEFAULT_UDP_PORT);
    printf("  -v, --verbose         Enable verbose output to stdout\n");
    printf("  -h, --help            Show this help message\n");
    printf("\nExample:\n");
    printf("  %s --rate 100 --cal 100 --host 127.0.0.1 --port 8889\n", program_name);
}

static void parse_args(int argc, char *argv[], IMUConfig *config)
{
    // Set defaults
    config->sample_rate_hz = DEFAULT_SAMPLE_RATE_HZ;
    config->calibration_samples = DEFAULT_CALIBRATION_SAMPLES;
    strncpy(config->udp_host, DEFAULT_UDP_HOST, sizeof(config->udp_host) - 1);
    config->udp_port = DEFAULT_UDP_PORT;
    config->verbose = 0;

    static struct option long_options[] = {
        {"rate",    required_argument, 0, 'r'},
        {"cal",     required_argument, 0, 'c'},
        {"host",    required_argument, 0, 'H'},
        {"port",    required_argument, 0, 'p'},
        {"verbose", no_argument,       0, 'v'},
        {"help",    no_argument,       0, 'h'},
        {0, 0, 0, 0}
    };

    int opt;
    int option_index = 0;

    while ((opt = getopt_long(argc, argv, "r:c:H:p:vh", long_options, &option_index)) != -1) {
        switch (opt) {
            case 'r':
                config->sample_rate_hz = atoi(optarg);
                if (config->sample_rate_hz < 1 || config->sample_rate_hz > 1000) {
                    fprintf(stderr, "Error: Sample rate must be between 1 and 1000 Hz\n");
                    exit(1);
                }
                break;
            case 'c':
                config->calibration_samples = atoi(optarg);
                if (config->calibration_samples < 10 || config->calibration_samples > 1000) {
                    fprintf(stderr, "Error: Calibration samples must be between 10 and 1000\n");
                    exit(1);
                }
                break;
            case 'H':
                strncpy(config->udp_host, optarg, sizeof(config->udp_host) - 1);
                break;
            case 'p':
                config->udp_port = atoi(optarg);
                if (config->udp_port < 1 || config->udp_port > 65535) {
                    fprintf(stderr, "Error: Port must be between 1 and 65535\n");
                    exit(1);
                }
                break;
            case 'v':
                config->verbose = 1;
                break;
            case 'h':
                print_usage(argv[0]);
                exit(0);
            default:
                print_usage(argv[0]);
                exit(1);
        }
    }
}

// Get current time in seconds (wall clock)
static double get_time_seconds(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

// Send JSON data via UDP
static int send_udp_json(int sockfd, struct sockaddr_in *dest_addr, const char *json)
{
    ssize_t sent = sendto(sockfd, json, strlen(json), 0,
                          (struct sockaddr *)dest_addr, sizeof(*dest_addr));
    return (sent > 0) ? 0 : -1;
}

int main(int argc, char *argv[])
{
    IMUConfig config;
    parse_args(argc, argv, &config);

    // Derive timing from single sample rate source
    int sample_period_us = 1000000 / config.sample_rate_hz;

    IIM42652_axis_t accel_data;
    IIM42652_axis_t gyro_data;

    float acc_x, acc_y, acc_z;
    float gyro_x, gyro_y, gyro_z;

    // Setup signal handlers for clean shutdown
    struct sigaction sa;
    sa.sa_handler = signal_handler;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = 0;
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGTERM, &sa, NULL);

    // Setup UDP socket
    int sockfd = socket(AF_INET, SOCK_DGRAM, 0);
    if (sockfd < 0) {
        perror("Failed to create UDP socket");
        return 1;
    }

    struct sockaddr_in dest_addr;
    memset(&dest_addr, 0, sizeof(dest_addr));
    dest_addr.sin_family = AF_INET;
    dest_addr.sin_port = htons(config.udp_port);
    if (inet_pton(AF_INET, config.udp_host, &dest_addr.sin_addr) <= 0) {
        fprintf(stderr, "Invalid UDP host address: %s\n", config.udp_host);
        close(sockfd);
        return 1;
    }

    if (config.verbose) {
        printf("IMU Fusion Daemon Starting\n");
        printf("  Sample Rate: %d Hz (period: %d us)\n", config.sample_rate_hz, sample_period_us);
        printf("  Calibration Samples: %d (%.1f seconds)\n",
               config.calibration_samples,
               (float)config.calibration_samples / config.sample_rate_hz);
        printf("  UDP Output: %s:%d\n", config.udp_host, config.udp_port);
    }

    // Initialize sensor
    iim42652_open();

    if (iim42652_avail()) {
        fprintf(stderr, "Sensor not found! Check your connections.\n");
        close(sockfd);
        return 1;
    }

    // ========== CALIBRATION PHASE ==========
    if (config.verbose) {
        printf("=== Calibration Phase ===\n");
        printf("Keep the sensor STILL for %.1f seconds...\n",
               (float)config.calibration_samples / config.sample_rate_hz);
    }

    // Send calibration start notification
    char json_buf[512];
    snprintf(json_buf, sizeof(json_buf),
             "{\"type\":\"calibration_start\",\"samples\":%d,\"duration_sec\":%.1f}",
             config.calibration_samples,
             (float)config.calibration_samples / config.sample_rate_hz);
    send_udp_json(sockfd, &dest_addr, json_buf);

    float gyro_sum_x = 0.0f, gyro_sum_y = 0.0f, gyro_sum_z = 0.0f;
    float accel_sum_x = 0.0f, accel_sum_y = 0.0f, accel_sum_z = 0.0f;

    for (int i = 0; i < config.calibration_samples && running; i++) {
        iim42652_ex_idle();
        iim42652_accelerometer_enable();
        iim42652_gyroscope_enable();

        usleep(sample_period_us);

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

        // Send calibration progress
        if ((i + 1) % (config.calibration_samples / 10) == 0 || i == config.calibration_samples - 1) {
            int progress = (i + 1) * 100 / config.calibration_samples;
            snprintf(json_buf, sizeof(json_buf),
                     "{\"type\":\"calibration_progress\",\"progress\":%d}",
                     progress);
            send_udp_json(sockfd, &dest_addr, json_buf);

            if (config.verbose) {
                printf("Calibrating... %d%%\n", progress);
            }
        }
    }

    if (!running) {
        if (config.verbose) printf("Interrupted during calibration\n");
        close(sockfd);
        iim42652_close();
        return 0;
    }

    // Calculate gyroscope offset (average bias at rest)
    FusionVector gyroscopeOffset = {
        {gyro_sum_x / config.calibration_samples,
         gyro_sum_y / config.calibration_samples,
         gyro_sum_z / config.calibration_samples}
    };

    // Calculate accelerometer offset (X and Y only, Z has gravity)
    FusionVector accelerometerOffset = {
        {accel_sum_x / config.calibration_samples,
         accel_sum_y / config.calibration_samples,
         0.0f}
    };

    // Send calibration complete notification
    snprintf(json_buf, sizeof(json_buf),
             "{\"type\":\"calibration_complete\","
             "\"gyro_offset\":[%.4f,%.4f,%.4f],"
             "\"accel_offset\":[%.4f,%.4f,%.4f]}",
             gyroscopeOffset.axis.x, gyroscopeOffset.axis.y, gyroscopeOffset.axis.z,
             accelerometerOffset.axis.x, accelerometerOffset.axis.y, accelerometerOffset.axis.z);
    send_udp_json(sockfd, &dest_addr, json_buf);

    if (config.verbose) {
        printf("=== Calibration Complete ===\n");
        printf("Gyro offset: X=%.3f, Y=%.3f, Z=%.3f deg/s\n",
               gyroscopeOffset.axis.x, gyroscopeOffset.axis.y, gyroscopeOffset.axis.z);
        printf("Accel offset: X=%.4f, Y=%.4f g\n",
               accelerometerOffset.axis.x, accelerometerOffset.axis.y);
        printf("============================\n\n");
    }

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

    // Initialize AHRS
    FusionAhrs ahrs;
    FusionAhrsInitialise(&ahrs);

    // Set AHRS settings
    const FusionAhrsSettings settings = {
        .convention = FusionConventionNwu,
        .gain = FUSION_GAIN,
        .gyroscopeRange = FUSION_GYRO_RANGE,
        .accelerationRejection = FUSION_ACCEL_REJECTION,
        .magneticRejection = 10.0f,
        .recoveryTriggerPeriod = 5 * config.sample_rate_hz,
    };
    FusionAhrsSetSettings(&ahrs, &settings);

    // Timing
    double previousTime = get_time_seconds();

    if (config.verbose) {
        printf("=== Main Loop Started ===\n");
    }

    // ========== MAIN LOOP ==========
    while (running) {
        iim42652_ex_idle();
        iim42652_accelerometer_enable();
        iim42652_gyroscope_enable();

        usleep(sample_period_us);

        iim42652_get_accel_data(&accel_data);
        iim42652_get_gyro_data(&gyro_data);

        iim42652_accelerometer_disable();
        iim42652_gyroscope_disable();
        iim42652_idle();

        // Convert to physical units (raw values)
        acc_x = (float)accel_data.x / 2048.0f;
        acc_y = (float)accel_data.y / 2048.0f;
        acc_z = (float)accel_data.z / 2048.0f;

        gyro_x = (float)gyro_data.x / 16.4f;
        gyro_y = (float)gyro_data.y / 16.4f;
        gyro_z = (float)gyro_data.z / 16.4f;

        // Store raw values before calibration
        float raw_gyro_x = gyro_x, raw_gyro_y = gyro_y, raw_gyro_z = gyro_z;
        float raw_acc_x = acc_x, raw_acc_y = acc_y, raw_acc_z = acc_z;

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
            deltaTime = 1.0f / config.sample_rate_hz;
        }

        // Update AHRS
        FusionAhrsUpdateNoMagnetometer(&ahrs, gyroscope, accelerometer, deltaTime);

        // Get outputs
        const FusionEuler euler = FusionQuaternionToEuler(FusionAhrsGetQuaternion(&ahrs));

        // Build JSON output
        snprintf(json_buf, sizeof(json_buf),
                 "{\"type\":\"imu_data\","
                 "\"raw_gyro\":[%.4f,%.4f,%.4f],"
                 "\"raw_accel\":[%.4f,%.4f,%.4f],"
                 "\"cal_gyro\":[%.4f,%.4f,%.4f],"
                 "\"cal_accel\":[%.4f,%.4f,%.4f],"
                 "\"roll\":%.2f,\"pitch\":%.2f,\"yaw\":%.2f,"
                 "\"dt\":%.6f}",
                 raw_gyro_x, raw_gyro_y, raw_gyro_z,
                 raw_acc_x, raw_acc_y, raw_acc_z,
                 gyroscope.axis.x, gyroscope.axis.y, gyroscope.axis.z,
                 accelerometer.axis.x, accelerometer.axis.y, accelerometer.axis.z,
                 euler.angle.roll, euler.angle.pitch, euler.angle.yaw,
                 deltaTime);

        send_udp_json(sockfd, &dest_addr, json_buf);

        if (config.verbose) {
            printf("Roll %+6.1f, Pitch %+6.1f, Yaw %+6.1f (dt=%.4f)\n",
                   euler.angle.roll, euler.angle.pitch, euler.angle.yaw, deltaTime);
        }
    }

    // Send shutdown notification
    snprintf(json_buf, sizeof(json_buf), "{\"type\":\"shutdown\"}");
    send_udp_json(sockfd, &dest_addr, json_buf);

    if (config.verbose) {
        printf("\nShutdown signal received. Cleaning up...\n");
    }

    close(sockfd);
    iim42652_close();
    return 0;
}
