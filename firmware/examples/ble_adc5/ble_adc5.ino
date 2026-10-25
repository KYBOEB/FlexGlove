/*
  FlexGlove — эталонный скетч: 5 сенсоров → строка "v1;v2;v3;v4;v5" по BLE, 50 Гц (протокол v0.2).

  * Значения — сырые analogRead() (0…4095). Вся обработка (фильтр, калибровка) — в ПК-приложении.
  * Одно BLE-уведомление = одна строка. Строка до ~25 байт, поэтому увеличиваем MTU.
  * Тот же поток идёт в Serial — можно подключиться по USB («Настройки → Порт»).
  * MOCK 1 — без сенсоров: плавно «сгибает» пальцы по очереди.

  Плата: ESP32-S3 или ESP32, Arduino-ESP32 core 2.x/3.x, BLE-библиотека из состава core.
  ВНИМАНИЕ: эталон не компилировался под конкретную плату — проверьте пины.
*/
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>

#define MOCK 0
#define DEVICE_NAME "FlexGlove-01"
#define SERVICE_UUID "6e7a0001-6c6f-7665-2d66-6c6578676c76"
#define DATA_UUID    "6e7a0002-6c6f-7665-2d66-6c6578676c76"

// Только входы ADC1 (ADC2 конфликтует с Wi-Fi). ESP32-S3: GPIO1…GPIO10. Обычный ESP32: GPIO32…39.
const int PINS[] = {1, 2, 3, 4, 5};          // большой, указательный, средний, безымянный, мизинец
const int N = sizeof(PINS) / sizeof(PINS[0]);
const uint32_t PERIOD_MS = 20;               // 50 Гц

BLECharacteristic *dataChar = nullptr;
volatile bool connected = false;

class Callbacks : public BLEServerCallbacks {
  void onConnect(BLEServer *) override { connected = true; }
  void onDisconnect(BLEServer *) override { connected = false; BLEDevice::startAdvertising(); }
};

int readSensor(int ch) {
#if MOCK
  float phase = (millis() % 10000) / 10000.0f * 5.0f;      // каждые 2 с — следующий палец
  float bend = (int)phase == ch ? sinf((phase - ch) * PI) : 0.0f;
  return 213 + (int)(700 * bend) + random(-3, 4);
#else
  return analogRead(PINS[ch]);
#endif
}

void setup() {
  Serial.begin(115200);
  analogReadResolution(12);
  BLEDevice::init(DEVICE_NAME);
  BLEDevice::setMTU(185);                   // строки длиннее 20 байт
  BLEServer *server = BLEDevice::createServer();
  server->setCallbacks(new Callbacks());
  BLEService *service = server->createService(SERVICE_UUID);
  dataChar = service->createCharacteristic(DATA_UUID, BLECharacteristic::PROPERTY_NOTIFY | BLECharacteristic::PROPERTY_READ);
  dataChar->addDescriptor(new BLE2902());
  service->start();
  BLEAdvertising *adv = BLEDevice::getAdvertising();
  adv->addServiceUUID(SERVICE_UUID);
  adv->setScanResponse(true);
  BLEDevice::startAdvertising();
}

void loop() {
  static uint32_t next = millis();
  if ((int32_t)(millis() - next) < 0) return;
  next += PERIOD_MS;                         // ровный шаг без накопления ошибки

  char line[64];
  int len = 0;
  for (int ch = 0; ch < N; ch++) {
    len += snprintf(line + len, sizeof(line) - len, ch ? ";%d" : "%d", readSensor(ch));
  }
  Serial.println(line);
  if (connected) {
    dataChar->setValue((uint8_t *)line, len);
    dataChar->notify();
  }
}
