#pragma once

// ─── AP / Onboarding ──────────────────────────────────────────────────────────
#define AP_SSID              "IncubatorSetup"
#define AP_PASSWORD          "incubator"       // min 8 chars for WPA2; set "" for open
#define AP_CHANNEL           6
#define AP_IP_ADDR           "192.168.4.1"
#define SETUP_PORTAL_PORT    80

// ─── UART host communication ──────────────────────────────────────────────────
// The Python backend talks JSON over serial. By default we use UART0 (GPIO1/GPIO3),
// which is the same UART used by the USB-to-serial adapter on the ESP32-CAM board.
// Set USE_SERIAL2 to true and wire a separate TTL adapter to GPIO16/GPIO17 instead.
#define HOST_BAUD            115200
#define USE_SERIAL2          false
#define PIN_UART2_RX         16   // conflicts with PSRAM on AI-Thinker; check your module
#define PIN_UART2_TX         17

// ─── Hardware GPIO ─────────────────────────────────────────────────────────────
// AI-Thinker ESP32-CAM GPIOs not consumed by the camera or PSRAM: 12, 13, 14, 15.
// GPIO12 must be LOW at boot on modules with 3.3 V flash (eFuse MTDI).
// GPIO0  is used for flash mode; keep it free at boot.
// GPIO2  drives the on-board white LED and is also used by the camera interface.
// Adjust these to match your actual wiring.

#define PIN_DHT              13   // DHT22 data line
#define PIN_HEATER           14   // Heater relay signal (active-LOW)
#define PIN_FAN              15   // Fan relay signal (active-LOW)
#define PIN_MOTOR_STEP       12   // Egg-turner stepper STEP pulse (or DC-motor PWM)
#define PIN_MOTOR_DIR        -1   // Egg-turner direction pin (-1 = not used)
#define PIN_LOCK             2    // Lock relay signal; also flashes on-board LED
#define PIN_CANDLE           4    // Candling flashlight (GPIO4 = on-board flash LED)

// ─── DHT sensor ───────────────────────────────────────────────────────────────
#define DHT_TYPE             DHT22

// ─── Relay logic ──────────────────────────────────────────────────────────────
// Most opto-isolated relay modules trigger when the input is pulled LOW.
#define RELAY_ON             LOW
#define RELAY_OFF            HIGH

// ─── Motor / egg-turner ───────────────────────────────────────────────────────
#define MOTOR_STEPS_PER_TURN 200    // 1.8° stepper = 200 full steps per revolution
#define MOTOR_STEP_DELAY_US  2000   // Microseconds between step pulses
#define MOTOR_TURN_REVOLUTIONS 1    // How many full revolutions per turn cycle

// ─── Camera (AI-Thinker OV2640) ───────────────────────────────────────────────
#define CAM_PIN_PWDN         32
#define CAM_PIN_RESET        -1
#define CAM_PIN_XCLK         0
#define CAM_PIN_SIOD         26
#define CAM_PIN_SIOC         27
#define CAM_PIN_D7           35
#define CAM_PIN_D6           34
#define CAM_PIN_D5           39
#define CAM_PIN_D4           36
#define CAM_PIN_D3           21
#define CAM_PIN_D2           19
#define CAM_PIN_D1           18
#define CAM_PIN_D0           5
#define CAM_PIN_VSYNC        25
#define CAM_PIN_HREF         23
#define CAM_PIN_PCLK         22

// ─── NVS (Preferences) ────────────────────────────────────────────────────────
#define NVS_NAMESPACE        "incubator"
#define NVS_KEY_SSID         "wifi_ssid"
#define NVS_KEY_PASS         "wifi_pass"
#define NVS_KEY_DEVNAME      "device_name"
#define NVS_KEY_SETUP_DONE   "setup_done"

// ─── Timeouts / misc ──────────────────────────────────────────────────────────
#define WIFI_CONNECT_TIMEOUT_MS   20000
#define UART_LINE_BUF_SIZE        512
#define WIFI_SCAN_TIMEOUT_MS      5000
#define AP_WATCHDOG_MS            300000  // 5 min: reboot if nobody completes setup
