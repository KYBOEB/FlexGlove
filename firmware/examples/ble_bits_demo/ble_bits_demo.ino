/*
  FlexGlove — эталонный скетч для проверки связи с ПК-приложением (этап 0).

  Что делает:
    * читает 2 гибких сенсора через делители напряжения (входы ADC1);
    * превращает напряжение в состояние пальца 0/1 с гистерезисом;
    * 20 раз в секунду отправляет строку вида "01" по BLE (notify) и в Serial.
  Режим MOCK = 1 — без сенсоров: перебирает 00 → 01 → 00 → 10 → 00 → 11.

  ПК-приложение подписывается на ЛЮБУЮ notify-характеристику, поэтому UUID ниже
  не обязательны, но лучше оставить их — они из docs/protocol.md.

  Плата: ESP32-S3 (или обычный ESP32), Arduino-ESP32 core 2.x/3.x, библиотека BLE из состава core.
  ВНИМАНИЕ: эталон написан без компиляции под конкретную плату — проверьте пины.
*/
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>

#define MOCK 1                      // 1 — имитация, 0 — реальные сенсоры
#define DEVICE_NAME "FlexGlove-01"

#define SERVICE_UUID "6e7a0001-6c6f-7665-2d66-6c6578676c76"
#define DATA_UUID    "6e7a0002-6c6f-7665-2d66-6c6578676c76"

// ESP32-S3: ADC1 = GPIO1..GPIO10. Обычный ESP32: ADC1 = GPIO32..39 (например, 34 и 35).
const int SENSOR_PINS[] = {1, 2};
const int N_CH = sizeof(SENSOR_PINS) / sizeof(SENSOR_PINS[0]);

// Пороги в мВ с гистерезисом — подобрать по реальным сенсорам (см. Serial-вывод).
// Если при сгибании напряжение РАСТЁТ, поменяйте местами логику в updateState().
const int BENT_BELOW_MV     = 1400;  // ниже — палец согнут
const int STRAIGHT_ABOVE_MV = 1700;  // выше — палец разогнут

BLECharacteristic *dataChar = nullptr;
bool clientConnected = false;
int state[8] = {0};

class ServerCallbacks : public BLEServerCallbacks {
  void onConnect(BLEServer *s) override { clientConnected = true; }
  void onDisconnect(BLEServer *s) override {
    clientConnected = false;
    BLEDevice::startAdvertising();   // снова видны для поиска
  }
};

int readMilliVolts(int pin) {
  long sum = 0;
  for (int i = 0; i < 16; i++) sum += analogReadMilliVolts(pin);  // усреднение 16 замеров
  return sum / 16;
}

void updateState(int ch, int mv) {
  if (mv < BENT_BELOW_MV) state[ch] = 1;
  else if (mv > STRAIGHT_ABOVE_MV) state[ch] = 0;
  // между порогами — оставляем прежнее значение (гистерезис)
}

void setup() {
  Serial.begin(115200);
  analogReadResolution(12);

  BLEDevice::init(DEVICE_NAME);
  BLEServer *server = BLEDevice::createServer();
  server->setCallbacks(new ServerCallbacks());
  BLEService *service = server->createService(SERVICE_UUID);
  dataChar = service->createCharacteristic(
      DATA_UUID, BLECharacteristic::PROPERTY_NOTIFY | BLECharacteristic::PROPERTY_READ);
  dataChar->addDescriptor(new BLE2902());
  service->start();

  BLEAdvertising *adv = BLEDevice::getAdvertising();
  adv->addServiceUUID(SERVICE_UUID);
  adv->setScanResponse(true);
  BLEDevice::startAdvertising();
  Serial.println("FlexGlove: advertising...");
}

void loop() {
  static unsigned long lastSend = 0;
  static int mockStep = 0;
  static unsigned long lastMock = 0;
  const char *MOCK_SEQ[] = {"00", "01", "00", "10", "00", "11"};

  if (millis() - lastSend < 50) return;   // 20 Гц
  lastSend = millis();

  char msg[12] = {0};
  if (MOCK) {
    if (millis() - lastMock > 1500) { lastMock = millis(); mockStep = (mockStep + 1) % 6; }
    strcpy(msg, MOCK_SEQ[mockStep]);
  } else {
    Serial.print("# ");                    // строки с # — отладка, приложение их пропускает
    for (int ch = 0; ch < N_CH; ch++) {
      int mv = readMilliVolts(SENSOR_PINS[ch]);
      updateState(ch, mv);
      Serial.printf("ch%d=%dmV ", ch + 1, mv);
      msg[ch] = state[ch] ? '1' : '0';
    }
    Serial.println();
  }
  Serial.println(msg);

  dataChar->setValue((uint8_t *)msg, strlen(msg));
  if (clientConnected) dataChar->notify();
}
