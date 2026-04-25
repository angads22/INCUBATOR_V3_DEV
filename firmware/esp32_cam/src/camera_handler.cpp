#include "camera_handler.h"
#include "config.h"

#include "esp_camera.h"

CameraHandler::CameraHandler() {}

bool CameraHandler::begin() {
    _ready = configCamera();
    if (_ready) {
        Serial.println("[CAM] OV2640 initialised OK");
    } else {
        Serial.println("[CAM] OV2640 init FAILED — camera commands will error");
    }
    return _ready;
}

// ─── captureAndSave ───────────────────────────────────────────────────────────
// Grabs a JPEG frame and sends it over Serial using a simple length-prefixed
// binary protocol so the Python host can reassemble the bytes:
//
//   JSON line (normal response):
//     {"ok":true,"value":"img_00042","size":14832}\n
//
//   Immediately followed by raw binary frame:
//     [4-byte little-endian length][JPEG bytes]
//
// The Python CameraService can detect the 4-byte header and read that many
// additional bytes to obtain the JPEG. If the host doesn't support binary
// frames it can simply ignore everything after \n until the next command.

String CameraHandler::captureAndSave() {
    if (!_ready) return "";

    camera_fb_t* fb = esp_camera_fb_get();
    if (!fb) {
        Serial.println("[CAM] Frame buffer grab failed");
        return "";
    }

    _frameCount++;
    char ref[24];
    snprintf(ref, sizeof(ref), "img_%05lu", (unsigned long)_frameCount);

    // Emit the binary frame on the SAME serial port, right after the JSON
    // line will be written by UartHandler. We pre-write a 4-byte size header.
    uint32_t len = fb->len;
    uint8_t header[4] = {
        (uint8_t)(len & 0xFF),
        (uint8_t)((len >> 8)  & 0xFF),
        (uint8_t)((len >> 16) & 0xFF),
        (uint8_t)((len >> 24) & 0xFF),
    };

    // The JSON response is written by UartHandler after we return the ref.
    // We stash the frame pointer and emit binary right after setup.
    // Simpler approach: write everything here, bypass UartHandler response.
    // We write the JSON + binary in one go so the host sees them atomically.
    char jsonLine[128];
    snprintf(jsonLine, sizeof(jsonLine),
             "{\"ok\":true,\"value\":\"%s\",\"size\":%lu}", ref, (unsigned long)len);

    Serial.println(jsonLine);         // JSON first (ends with \n)
    Serial.write(header, 4);          // then 4-byte LE length
    Serial.write(fb->buf, fb->len);   // then raw JPEG bytes
    Serial.flush();

    esp_camera_fb_return(fb);

    // Return a sentinel so UartHandler knows we already wrote the response
    return String("__raw_sent__");
}

// ─── Private ──────────────────────────────────────────────────────────────────

bool CameraHandler::configCamera() {
    camera_config_t cfg = {};

    cfg.ledc_channel = LEDC_CHANNEL_0;
    cfg.ledc_timer   = LEDC_TIMER_0;
    cfg.pin_d0       = CAM_PIN_D0;
    cfg.pin_d1       = CAM_PIN_D1;
    cfg.pin_d2       = CAM_PIN_D2;
    cfg.pin_d3       = CAM_PIN_D3;
    cfg.pin_d4       = CAM_PIN_D4;
    cfg.pin_d5       = CAM_PIN_D5;
    cfg.pin_d6       = CAM_PIN_D6;
    cfg.pin_d7       = CAM_PIN_D7;
    cfg.pin_xclk     = CAM_PIN_XCLK;
    cfg.pin_pclk     = CAM_PIN_PCLK;
    cfg.pin_vsync    = CAM_PIN_VSYNC;
    cfg.pin_href     = CAM_PIN_HREF;
    cfg.pin_sscb_sda = CAM_PIN_SIOD;
    cfg.pin_sscb_scl = CAM_PIN_SIOC;
    cfg.pin_pwdn     = CAM_PIN_PWDN;
    cfg.pin_reset    = CAM_PIN_RESET;
    cfg.xclk_freq_hz = 20000000;
    cfg.pixel_format = PIXFORMAT_JPEG;

    // Use PSRAM for larger frames if available
    if (psramFound()) {
        cfg.frame_size   = FRAMESIZE_UXGA;  // 1600×1200
        cfg.jpeg_quality = 10;
        cfg.fb_count     = 2;
        cfg.fb_location  = CAMERA_FB_IN_PSRAM;
        cfg.grab_mode    = CAMERA_GRAB_LATEST;
    } else {
        cfg.frame_size   = FRAMESIZE_SVGA;  // 800×600
        cfg.jpeg_quality = 12;
        cfg.fb_count     = 1;
        cfg.fb_location  = CAMERA_FB_IN_DRAM;
        cfg.grab_mode    = CAMERA_GRAB_WHEN_EMPTY;
    }

    esp_err_t err = esp_camera_init(&cfg);
    if (err != ESP_OK) {
        Serial.printf("[CAM] esp_camera_init error 0x%x\n", err);
        return false;
    }

    // Tuning for incubator (warm, dim environment)
    sensor_t* s = esp_camera_sensor_get();
    if (s) {
        s->set_brightness(s, 1);
        s->set_saturation(s, 0);
        s->set_gainceiling(s, GAINCEILING_4X);
        s->set_whitebal(s, 1);
        s->set_awb_gain(s, 1);
        s->set_wb_mode(s, 0);  // auto
        s->set_exposure_ctrl(s, 1);
        s->set_aec2(s, 1);
    }
    return true;
}
