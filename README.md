<h1 align=center>Авто-Синхрон чеков из <code>ЮKassa</code> в <code>Мой Налог</code> для самозанятых (НПД)</h1>

> **Автоматическая синхронизация** платежей из личного кабинета ЮKassa в приложение "Мой Налог"

<p align=center>Данный репозиторий был создан и поддерживается в связи с тем, что <b>ЮKassa</b> остановили свой сервис авто-отправки чеков в "Мой Налог"</p>

---

## 🚀 Возможности

- ✅ Автоматическая синхронизация платежей при запуске контейнера
- ✅ Два способа авторизации в "Мой Налог": логин+пароль или refresh token (для входа через Госуслуги)
- ✅ Автоматическое аннулирование чеков при возвратах в ЮKassa
- ✅ Telegram-уведомления о результатах синхронизации
- ✅ Email-уведомления при наличии активности
- ✅ Регулярное выполнение по расписанию (настраивается через .env)
- ✅ Валидация конфигурации при старте
- ✅ Автоматические повторы при сбоях сети
- ✅ Защита от дублирования чеков
- ✅ Сохранение состояния синхронизации
- ✅ Подробное логирование

## 📋 Требования

- Docker
- Учетные данные ЮKassa (Shop ID + API ключ)
- Учетные данные Мой Налог (логин + пароль **или** refresh token при входе через Госуслуги)

---

## 🔧 Установка

### 1. Устанавливаем Docker
```bash
sudo curl -fsSL https://get.docker.com | sh
```

### 2. Создаем папку `/opt/yookassa-to-mynalog` и переходим в нее (а так же создадим папку `logs` внутри)
```bash
sudo mkdir -p /opt/yookassa-to-mynalog/logs && cd /opt/yookassa-to-mynalog
```

### 3. Скачиваем файлы `.env.example` (его сразу ренеймим в `.env`) и `docker-compose.yml`
```bash
sudo wget -O .env https://raw.githubusercontent.com/grandvan709/yookassa-to-mynalog/refs/heads/master/.env.example && \
sudo wget -O docker-compose.yml https://raw.githubusercontent.com/grandvan709/yookassa-to-mynalog/refs/heads/master/docker-compose.yml
```

### 4. Заполняем файл `.env` необходимыми значениями (см раздел "Конфигурация")
```bash
sudo nano .env
```

## ⚙️ Конфигурация

### Обязательные переменные

<table>
  <tr>
    <th>Переменная</th>
    <th>Описание</th>
  </tr>
  <tr>
    <td><code>YOOKASSA_SHOP_ID</code></td>
    <td>ID магазина в ЮKassa</td>
  </tr>
  <tr>
    <td><code>YOOKASSA_API_KEY</code></td>
    <td>API ключ ЮKassa</td>
  </tr>
</table>

### Авторизация в "Мой Налог"

Способ авторизации выбирается переменной `MOY_NALOG_AUTH_METHOD` — `password` (по умолчанию) или `refresh`.

| Переменная | По умолчанию | Описание |
|-----------|:----------:|---------|
| `MOY_NALOG_AUTH_METHOD` | `password` | Способ авторизации: `password` или `refresh` |

#### Способ 1 — по логину и паролю (`MOY_NALOG_AUTH_METHOD=password`)

Логин и пароль от "Мой Налог" такие же, как и от личного кабинета налоговой физлиц.

<table>
  <tr>
    <th>Переменная</th>
    <th>Описание</th>
  </tr>
  <tr>
    <td><code>MOY_NALOG_LOGIN</code></td>
    <td>ИНН в Мой Налог</td>
  </tr>
  <tr>
    <td><code>MOY_NALOG_PASSWORD</code></td>
    <td>Пароль в Мой Налог</td>
  </tr>
</table>

#### Способ 2 — по refresh token (`MOY_NALOG_AUTH_METHOD=refresh`)

Подходит для аккаунтов, зарегистрированных через **Госуслуги** — у них нет пароля.

<table>
  <tr>
    <th>Переменная</th>
    <th>Описание</th>
  </tr>
  <tr>
    <td><code>MOY_NALOG_REFRESH_TOKEN</code></td>
    <td>Бессрочный refreshToken из браузера</td>
  </tr>
  <tr>
    <td><code>DEVICE_ID</code></td>
    <td>deviceId из браузера (тот, для которого выдан refreshToken)</td>
  </tr>
</table>

**Как получить `refreshToken` и `DEVICE_ID`:**

1. Войдите на [lknpd.nalog.ru](https://lknpd.nalog.ru) через Госуслуги.
2. Откройте DevTools браузера (`F12`) → вкладка **Network** (включите *Preserve log*).
3. Найдите запрос `auth/esia` (или `auth/token`) → вкладка **Response**.
4. Скопируйте `refreshToken` в `MOY_NALOG_REFRESH_TOKEN`, а `deviceInfo.sourceDeviceId` — в `DEVICE_ID`.

> В режиме `refresh` переменные `MOY_NALOG_LOGIN` и `MOY_NALOG_PASSWORD` не нужны. `refreshToken` привязан к `deviceId`, поэтому `DEVICE_ID` обязательно брать из браузера — автогенерация из ИНН в этом режиме не подойдёт.

### Опциональные переменные

| Переменная | По умолчанию | Описание |
|-----------|:----------:|---------|
| `TZ` | `Europe/Moscow` | Часовой пояс контейнера ([список](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones)) |
| `DEVICE_ID` | Генерация хеша из ИНН (21 символ) | ID устройства для авторизации в "Мой Налог". В режиме `password` — опционально; в режиме `refresh` — обязателен (берётся из браузера) |
| `SYNC_START_DATE` | -24ч | Дата начала синхронизации (YYYY-MM-DD) |
| `INCOME_DESCRIPTION_TEMPLATE` | `Платеж #{description}` | Шаблон описания дохода (см. ниже) |
| `CRON_SCHEDULE` | `0 */4 * * *` | Расписание cron (каждые 4 часа) |
| `YOOKASSA_NALOG_PROXY` | — | SOCKS5 прокси для ЮKassa и ФНС (для серверов вне РФ). Формат: `socks5h://user:pass@host:1080` |

### Telegram-уведомления (опционально)

После каждой синхронизации бот отправляет итоговое сообщение в указанный чат/тред. Если переменные не заданы — скрипт работает как прежде.

| Переменная | Описание |
|-----------|---------|
| `TELEGRAM_BOT_TOKEN` | Токен бота (получить у @BotFather) |
| `TELEGRAM_CHAT_ID` | ID чата/группы (узнать через @userinfobot или @getidsbot) |
| `TELEGRAM_THREAD_ID` | ID топика в супергруппе (опционально) |
| `TELEGRAM_PROXY` | SOCKS5 прокси для серверов без прямого доступа к Telegram (опционально) |

### Email-уведомления (опционально)

Письмо приходит **только при наличии активности** — новых платежей, возвратов или ошибок. Если событий нет — письмо не отправляется (в отличие от Telegram). Можно использовать параллельно с Telegram или отдельно.

| Переменная | По умолчанию | Описание |
|-----------|:----------:|---------|
| `SMTP_HOST` | — | SMTP сервер (например `smtp.gmail.com`, `smtp.yandex.ru`) |
| `SMTP_PORT` | `587` | Порт SMTP |
| `SMTP_USER` | — | Логин SMTP (обычно email) |
| `SMTP_PASSWORD` | — | Пароль SMTP (для Gmail/Yandex — пароль приложения) |
| `SMTP_FROM_EMAIL` | = `SMTP_USER` | Email отправителя |
| `SMTP_FROM_NAME` | — | Имя отправителя |
| `SMTP_TO_EMAIL` | — | Email получателя уведомлений |
| `SMTP_USE_TLS` | `true` | STARTTLS (true для 587, false для 465) |
| `EMAIL_SUBJECT` | `Синхронизация чеков в налоговой` | Тема письма |

### Переменные шаблона INCOME_DESCRIPTION_TEMPLATE

В шаблоне описания дохода можно использовать следующие переменные:

| Переменная | Описание |
|-----------|---------|
| `{description}` | Описание платежа из ЮKassa, либо ID платежа если описания нет |
| `{id}` | ID платежа в ЮKassa (UUID) |
| `{payment_description}` | Только описание платежа (пустая строка, если описания нет) |
| `{order_number}` | Номер счёта/заказа из ЮKassa (например: `1227418-1`) |
| `{invoice_id}` | ID счёта из invoice_details API (пустая строка, если нет) |
| `{customer_name}` | Название/имя из счёта ЮKassa |
| `{amount}` | Сумма платежа |
| `{merchant_customer_id}` | ID покупателя в вашей системе |

### Примеры INCOME_DESCRIPTION_TEMPLATE

```env
INCOME_DESCRIPTION_TEMPLATE='Платеж #{description}'                                    # описание платежа, или ID если нет описания
INCOME_DESCRIPTION_TEMPLATE='Оплата по счёту №{order_number}'                          # номер счёта из ЮKassa
INCOME_DESCRIPTION_TEMPLATE='{customer_name} — счёт №{order_number}'                   # название + номер счёта
INCOME_DESCRIPTION_TEMPLATE='Оплата услуг: {payment_description} ({amount} руб.)'      # описание + сумма
INCOME_DESCRIPTION_TEMPLATE='Платеж {id}'                                              # ID платежа (UUID)
```

### Примеры CRON_SCHEDULE

```env
CRON_SCHEDULE='0 * * * *'        # каждый час
CRON_SCHEDULE='0 */6 * * *'      # каждые 6 часов
CRON_SCHEDULE='0 12 * * *'       # один раз в день в 12:00
CRON_SCHEDULE='*/30 * * * *'     # каждые 30 минут
CRON_SCHEDULE='0 0 * * 0'        # один раз в неделю в воскресенье
```

**Формат cron:** `минуты часы дни_месяца месяцы дни_недели`

## 🚀 Запуск

### Первый запуск
```bash
cd /opt/yookassa-to-mynalog && sudo docker compose up -d
```

### Проверка логов
```bash
cd /opt/yookassa-to-mynalog && sudo docker compose logs -f -t
```

### Остановка
```bash
cd /opt/yookassa-to-mynalog && sudo docker compose down
```

### Перезагрузка
```bash
cd /opt/yookassa-to-mynalog && sudo docker compose down && sudo docker compose up -d
```

---

## 📊 Структура логов

```
2026-04-01 11:00:01,915 - INFO - Начало синхронизации...
2026-04-01 11:00:01,915 - INFO - Последняя синхронизация: 2026-03-31T10:47:18.218Z
2026-04-01 11:00:02,491 - INFO - ✓ Найдено новых платежей: 3
2026-04-01 11:00:02,877 - INFO - HTTP Request: POST https://lknpd.nalog.ru/api/v1/auth/lkfl "HTTP/1.1 200 OK"
2026-04-01 11:00:02,880 - INFO - ✓ Успешная авторизация в Мой Налог.
2026-04-01 11:00:04,276 - INFO - HTTP Request: POST https://lknpd.nalog.ru/api/v1/income "HTTP/1.1 200 OK"
2026-04-01 11:00:04,278 - INFO - ✓ Доход успешно зарегистрирован: 250.0 руб. за 'Платеж: 1' (чек: 1234567890)
2026-04-01 11:00:05,131 - INFO - HTTP Request: POST https://lknpd.nalog.ru/api/v1/income "HTTP/1.1 200 OK"
2026-04-01 11:00:05,132 - INFO - ✓ Доход успешно зарегистрирован: 500.0 руб. за 'Платеж: 2' (чек: 0987654321)
2026-04-01 11:00:07,283 - INFO - HTTP Request: POST https://lknpd.nalog.ru/api/v1/income "HTTP/1.1 200 OK"
2026-04-01 11:00:07,289 - INFO - ✓ Доход успешно зарегистрирован: 360.0 руб. за 'Платеж: 3' (чек: 3216549870)
2026-04-01 11:00:07,289 - INFO - Результат платежей: успешно=3, ошибок=0
2026-04-01 11:00:07,471 - INFO - ✓ Новых возвратов не найдено.
2026-04-01 11:00:07,719 - INFO - ✓ Уведомление отправлено в Telegram.
2026-04-01 11:00:07,720 - INFO - Синхронизация завершена.
```

### Обработка возвратов

При каждой синхронизации, помимо новых платежей, проверяются возвраты в ЮKassa. Если платёж был ранее зарегистрирован как доход — соответствующий чек в "Мой Налог" автоматически аннулируется с причиной "Возврат средств".

> **Важно:** аннулирование работает только для платежей, зарегистрированных **после обновления** до версии с поддержкой возвратов. Для более ранних платежей чеки при возврате нужно аннулировать вручную в ЛК налоговой.

---

## 💡 Обновление ПО

### 1. Переходим в нашу папку
```bash
cd /opt/yookassa-to-mynalog
```

### 2. Останавливаем контейнер
```bash
sudo docker compose down
```

### 3. Скачиваем новый образ
```bash
sudo docker compose pull
```

### 4. Запускаем контейнер и смотрим логи после запуска новой версии
```bash
sudo docker compose up -d && sudo docker compose logs -f -t
```

### 5. Проверка docker-compose.yml и прочих файлов
Перед обновлениями и запусками - убедитесь, что ваши файлы **docker-compose.yml** и **.env** *(и прочие, которые могут быть в будущем)* соответствуют последним версиям из репозитория!

> Чтобы не писать `sudo` перед каждой командой `docker` - нужно внести пользователя, из под которого вы работаете, в группу **docker** следующей командой: `sudo usermod -aG docker <username>`. А затем перезайти на сервер.
---

> **Ставь ⭐** и не пропусти регулярные обновления для поддержания актуальности скрипта и оптимальной автоматизации

> USDT TRC20: TL6gHETnKqNWV4D6GjiKKahkBsAwcyWfo8 | [ЮKassa (руб.)](https://yookassa.ru/my/i/aZUoMtbfNgP8/l)

<p align=center>
    <a href="https://t.me/grand_van" target="_blank" rel="noopener noreferrer">
        <img src="https://img.shields.io/badge/Telegram-GrandVan-purple?logo=telegram&logoColor=white&labelColor=blue" alt="Chat me on Telegram">
    </a>
</p>