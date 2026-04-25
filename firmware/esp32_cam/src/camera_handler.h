#pragma once

#include <Arduino.h>

class CameraHandler {
public:
    CameraHandler();

    // Must be called in setup(). Returns false if camera init fails.
    bool begin();

    bool ready() const { return _ready; }

    // Capture a JPEG frame, base64-encode it, and return a short reference
    // token that the Python host uses as image_ref.
    // The actual JPEG bytes are written to Serial as a separate binary frame
    // immediately after the JSON response — see captureAndSave().
    String captureAndSave();

private:
    bool configCamera();

    bool     _ready = false;
    uint32_t _frameCount = 0;
};
