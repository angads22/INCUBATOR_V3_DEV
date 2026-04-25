#include "wifi_manager.h"
#include "config.h"

#include <WiFi.h>
#include <DNSServer.h>
#include <WebServer.h>
#include <ArduinoJson.h>

static WebServer  webServer(SETUP_PORTAL_PORT);
static DNSServer  dnsServer;
static const byte DNS_PORT = 53;

// ─── Constructor ──────────────────────────────────────────────────────────────

WifiManager::WifiManager() {}

// ─── Public ───────────────────────────────────────────────────────────────────

bool WifiManager::begin() {
    _prefs.begin(NVS_NAMESPACE, false);
    bool setupDone = _prefs.getBool(NVS_KEY_SETUP_DONE, false);
    String ssid    = _prefs.getString(NVS_KEY_SSID, "");
    _deviceName    = _prefs.getString(NVS_KEY_DEVNAME, "My Incubator");

    if (!setupDone || ssid.isEmpty()) {
        Serial.println("[WiFi] No saved credentials — starting setup AP");
        startAP();
        return false;
    }

    startStation();
    return (_mode == WifiMode::STATION);
}

void WifiManager::handleClient() {
    if (_mode == WifiMode::AP) {
        dnsServer.processNextRequest();
        webServer.handleClient();

        // Safety watchdog: reboot if AP has been up too long with no completion
        if (millis() - _apStartMs > AP_WATCHDOG_MS) {
            Serial.println("[WiFi] AP watchdog expired — rebooting");
            ESP.restart();
        }
    }
}

void WifiManager::resetAndReboot() {
    Preferences p;
    p.begin(NVS_NAMESPACE, false);
    p.clear();
    p.end();
    Serial.println("[WiFi] Credentials cleared — rebooting into setup mode");
    delay(500);
    ESP.restart();
}

// ─── Private ──────────────────────────────────────────────────────────────────

void WifiManager::startAP() {
    WiFi.mode(WIFI_AP);
    IPAddress ip, gw, sn;
    ip.fromString(AP_IP_ADDR);
    gw = ip;
    sn.fromString("255.255.255.0");
    WiFi.softAPConfig(ip, gw, sn);

    bool ok = WiFi.softAP(AP_SSID, strlen(AP_PASSWORD) > 0 ? AP_PASSWORD : nullptr, AP_CHANNEL);
    if (!ok) {
        Serial.println("[WiFi] softAP failed — check AP_PASSWORD (min 8 chars)");
    }

    Serial.printf("[WiFi] AP started: SSID=%s  IP=%s\n", AP_SSID, AP_IP_ADDR);

    // Redirect every DNS query to our IP so phones show "sign in" notification
    dnsServer.setErrorReplyCode(DNSReplyCode::NoError);
    dnsServer.start(DNS_PORT, "*", ip);

    startCaptivePortal();
    _mode = WifiMode::AP;
    _apStartMs = millis();
}

void WifiManager::startStation() {
    String ssid = _prefs.getString(NVS_KEY_SSID, "");
    String pass = _prefs.getString(NVS_KEY_PASS, "");

    Serial.printf("[WiFi] Connecting to SSID: %s\n", ssid.c_str());
    WiFi.mode(WIFI_STA);
    WiFi.begin(ssid.c_str(), pass.c_str());

    unsigned long t = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - t < WIFI_CONNECT_TIMEOUT_MS) {
        delay(250);
        Serial.print(".");
    }
    Serial.println();

    if (WiFi.status() == WL_CONNECTED) {
        Serial.printf("[WiFi] Connected — IP: %s\n", WiFi.localIP().toString().c_str());
        _mode = WifiMode::STATION;
    } else {
        Serial.println("[WiFi] Connection failed — falling back to AP mode");
        startAP();
    }
}

void WifiManager::startCaptivePortal() {
    // Captive-portal detection endpoints for iOS, Android, Windows
    webServer.on("/",                        HTTP_GET,  handleRoot);
    webServer.on("/scan",                    HTTP_GET,  handleScan);
    webServer.on("/save",                    HTTP_POST, handleSave);
    webServer.on("/hotspot-detect.html",     HTTP_GET,  handleRoot);  // iOS
    webServer.on("/generate_204",            HTTP_GET,  handleRoot);  // Android
    webServer.on("/connecttest.txt",         HTTP_GET,  handleRoot);  // Windows
    webServer.on("/ncsi.txt",                HTTP_GET,  handleRoot);  // Windows
    webServer.onNotFound(handleNotFound);
    webServer.begin();
    Serial.println("[WiFi] Captive portal started on " AP_IP_ADDR);
}

// ─── HTTP handlers (static) ───────────────────────────────────────────────────

void WifiManager::handleRoot() {
    webServer.send(200, "text/html", buildSetupPage());
}

void WifiManager::handleNotFound() {
    // Redirect unknown requests to our setup page — triggers captive portal UI
    webServer.sendHeader("Location", "http://" AP_IP_ADDR "/", true);
    webServer.send(302, "text/plain", "");
}

void WifiManager::handleScan() {
    webServer.send(200, "application/json", scanNetworksJson());
}

void WifiManager::handleSave() {
    String ssid       = webServer.arg("ssid");
    String password   = webServer.arg("password");
    String deviceName = webServer.arg("device_name");

    if (ssid.isEmpty()) {
        webServer.send(400, "application/json", "{\"ok\":false,\"error\":\"SSID required\"}");
        return;
    }
    if (deviceName.isEmpty()) deviceName = "My Incubator";

    Preferences p;
    p.begin(NVS_NAMESPACE, false);
    p.putString(NVS_KEY_SSID,   ssid);
    p.putString(NVS_KEY_PASS,   password);
    p.putString(NVS_KEY_DEVNAME, deviceName);
    p.putBool(NVS_KEY_SETUP_DONE, true);
    p.end();

    Serial.printf("[WiFi] Saved SSID=%s  device=%s — rebooting\n",
                  ssid.c_str(), deviceName.c_str());

    webServer.send(200, "application/json",
                   "{\"ok\":true,\"message\":\"Saved! Device is restarting...\"}");
    delay(1500);
    ESP.restart();
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

String WifiManager::scanNetworksJson() {
    int n = WiFi.scanNetworks(false, true);  // blocking scan, include hidden
    JsonDocument doc;
    JsonArray arr = doc.to<JsonArray>();
    for (int i = 0; i < n && i < 20; i++) {
        JsonObject net = arr.add<JsonObject>();
        net["ssid"]    = WiFi.SSID(i);
        net["rssi"]    = WiFi.RSSI(i);
        net["secure"]  = (WiFi.encryptionType(i) != WIFI_AUTH_OPEN);
    }
    WiFi.scanDelete();
    String out;
    serializeJson(doc, out);
    return out;
}

String WifiManager::buildSetupPage() {
    return R"rawhtml(<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Incubator Setup</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       background:#f0f4f8;min-height:100vh;display:flex;align-items:center;
       justify-content:center;padding:16px}
  .card{background:#fff;border-radius:16px;padding:28px 24px;
        max-width:420px;width:100%;box-shadow:0 4px 24px rgba(0,0,0,.1)}
  h1{font-size:1.4rem;color:#1a202c;margin-bottom:4px}
  .subtitle{color:#718096;font-size:.9rem;margin-bottom:24px}
  label{display:block;font-size:.85rem;font-weight:600;color:#4a5568;
        margin-bottom:4px;margin-top:16px}
  input,select{width:100%;padding:10px 12px;border:1px solid #e2e8f0;
               border-radius:8px;font-size:1rem;color:#2d3748;outline:none}
  input:focus,select:focus{border-color:#4299e1;box-shadow:0 0 0 3px rgba(66,153,225,.2)}
  .net-list{max-height:180px;overflow-y:auto;border:1px solid #e2e8f0;
            border-radius:8px;margin-top:4px}
  .net-item{display:flex;align-items:center;padding:10px 12px;cursor:pointer;
            border-bottom:1px solid #f7fafc;gap:10px}
  .net-item:last-child{border-bottom:none}
  .net-item:hover{background:#ebf8ff}
  .net-item.selected{background:#bee3f8}
  .ssid-name{flex:1;font-size:.95rem}
  .rssi-icon{font-size:1.1rem}
  .btn{display:block;width:100%;padding:12px;border:none;border-radius:8px;
       font-size:1rem;font-weight:600;cursor:pointer;margin-top:24px;
       background:#4299e1;color:#fff;transition:background .15s}
  .btn:hover{background:#3182ce}
  .btn:disabled{background:#a0aec0;cursor:not-allowed}
  .status{margin-top:14px;padding:10px 14px;border-radius:8px;font-size:.9rem;
          display:none}
  .status.ok{background:#c6f6d5;color:#276749}
  .status.err{background:#fed7d7;color:#9b2c2c}
  .scan-btn{font-size:.8rem;color:#4299e1;background:none;border:none;
            cursor:pointer;margin-left:8px;text-decoration:underline}
  .lock-icon{font-size:.8rem;color:#a0aec0}
</style>
</head>
<body>
<div class="card">
  <h1>&#x1F423; Incubator Setup</h1>
  <p class="subtitle">Connect your incubator to Wi-Fi to finish setup.</p>

  <label>Device Name</label>
  <input id="device_name" type="text" placeholder="e.g. Barn Incubator #1" value="My Incubator">

  <label>Wi-Fi Network <button class="scan-btn" onclick="scanNetworks()">&#x21BB; Scan</button></label>
  <div class="net-list" id="net-list">
    <div style="padding:12px;color:#a0aec0;font-size:.9rem">Scanning...</div>
  </div>
  <input id="ssid_hidden" type="hidden">

  <div id="password_row" style="display:none">
    <label>Wi-Fi Password</label>
    <input id="password" type="password" placeholder="Enter password">
  </div>

  <button class="btn" id="save-btn" onclick="save()" disabled>Save &amp; Connect</button>
  <div class="status" id="status"></div>
</div>

<script>
let selectedSecure = false;

function rssiIcon(rssi) {
  if (rssi >= -55) return '&#x1F4F6;';
  if (rssi >= -70) return '&#x1F4F5;';
  return '&#x26A0;';
}

function scanNetworks() {
  const list = document.getElementById('net-list');
  list.innerHTML = '<div style="padding:12px;color:#a0aec0;font-size:.9rem">Scanning...</div>';
  document.getElementById('save-btn').disabled = true;

  fetch('/scan')
    .then(r => r.json())
    .then(nets => {
      if (!nets.length) {
        list.innerHTML = '<div style="padding:12px;color:#a0aec0;font-size:.9rem">No networks found. Try again.</div>';
        return;
      }
      nets.sort((a, b) => b.rssi - a.rssi);
      list.innerHTML = nets.map(n =>
        `<div class="net-item" onclick="selectNet(this,'${encodeURIComponent(n.ssid)}',${n.secure})">
           <span class="rssi-icon">${rssiIcon(n.rssi)}</span>
           <span class="ssid-name">${escHtml(n.ssid)}</span>
           ${n.secure ? '<span class="lock-icon">&#x1F512;</span>' : ''}
         </div>`
      ).join('');
    })
    .catch(() => {
      list.innerHTML = '<div style="padding:12px;color:#e53e3e;font-size:.9rem">Scan failed. Try again.</div>';
    });
}

function selectNet(el, encodedSsid, secure) {
  document.querySelectorAll('.net-item').forEach(e => e.classList.remove('selected'));
  el.classList.add('selected');
  const ssid = decodeURIComponent(encodedSsid);
  document.getElementById('ssid_hidden').value = ssid;
  selectedSecure = secure;
  document.getElementById('password_row').style.display = secure ? 'block' : 'none';
  document.getElementById('save-btn').disabled = false;
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function save() {
  const ssid = document.getElementById('ssid_hidden').value;
  const pass = document.getElementById('password').value;
  const name = document.getElementById('device_name').value.trim() || 'My Incubator';
  const btn  = document.getElementById('save-btn');
  const stat = document.getElementById('status');

  if (!ssid) { showStatus('Please select a Wi-Fi network.', false); return; }
  if (selectedSecure && pass.length < 8) { showStatus('Password must be at least 8 characters.', false); return; }

  btn.disabled = true;
  btn.textContent = 'Connecting...';

  const body = new URLSearchParams({ssid, password: pass, device_name: name});
  fetch('/save', { method:'POST', body, headers:{'Content-Type':'application/x-www-form-urlencoded'} })
    .then(r => r.json())
    .then(d => {
      if (d.ok) {
        showStatus('&#x2705; ' + d.message, true);
        btn.textContent = 'Done!';
      } else {
        showStatus('&#x274C; ' + (d.error || 'Unknown error'), false);
        btn.disabled = false;
        btn.textContent = 'Save & Connect';
      }
    })
    .catch(() => {
      showStatus('&#x274C; Connection error. Try again.', false);
      btn.disabled = false;
      btn.textContent = 'Save & Connect';
    });
}

function showStatus(msg, ok) {
  const el = document.getElementById('status');
  el.innerHTML = msg;
  el.className = 'status ' + (ok ? 'ok' : 'err');
  el.style.display = 'block';
}

scanNetworks();
</script>
</body>
</html>)rawhtml";
}
