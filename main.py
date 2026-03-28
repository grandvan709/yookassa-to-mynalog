import asyncio
import json
import os
import logging
import httpx
import hashlib
from datetime import datetime, timedelta, timezone
from yookassa import Configuration, Payment
from tenacity import retry, stop_after_attempt, wait_exponential
import config
from telegram_notifier import TelegramNotifier

LOG_DIR = "logs"
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(f"{LOG_DIR}/sync.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)

def generate_device_id_from_login(login: str) -> str:
    return hashlib.sha256(login.encode('utf-8')).hexdigest()[:21]


class SafeFormatDict(dict):
    def __missing__(self, key):
        logging.warning(f"Неизвестная переменная в шаблоне: {{{key}}}")
        return f"{{{key}}}"


def build_template_vars(payment) -> dict:
    metadata = payment.metadata or {}

    invoice_id = ""
    if payment.invoice_details and hasattr(payment.invoice_details, 'id'):
        invoice_id = payment.invoice_details.id or ""

    order_number = metadata.get("orderNumber") or metadata.get("dashboardInvoiceOriginalNumber") or ""
    customer_name = metadata.get("custName") or metadata.get("customerNumber") or ""

    return SafeFormatDict({
        "description": payment.description or payment.id,
        "id": payment.id,
        "payment_description": payment.description or "",
        "order_number": order_number,
        "invoice_id": invoice_id,
        "customer_name": customer_name,
        "amount": payment.amount.value,
        "merchant_customer_id": getattr(payment, 'merchant_customer_id', "") or "",
    })

class MoyNalogAPI:
    def __init__(self, login, password):
        self.login = login
        self.password = password
        self.token = None
        
        if config.DEVICE_ID:
            self.device_id = config.DEVICE_ID
            logging.info(f"Используется deviceId из .env: {self.device_id}")
        else:
            self.device_id = generate_device_id_from_login(login)
            logging.info(f"Сгенерирован deviceId на основе ИНН: {self.device_id}")

        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
        
        self.headers = {
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'ru,en-US;q=0.9,en;q=0.8',
            'Connection': 'keep-alive',
            'Content-Type': 'application/json',
            'Origin': 'https://lknpd.nalog.ru/',
            'Referer': 'https://lknpd.nalog.ru/',
            'User-Agent': self.user_agent,
            'sec-ch-ua': '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin'
        }
        
        self.client = httpx.AsyncClient(headers=self.headers, timeout=30.0)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def authenticate(self):
        url = "https://lknpd.nalog.ru/api/v1/auth/lkfl"
        payload = {
            "username": self.login,
            "password": self.password,
            "deviceInfo": {
                "sourceDeviceId": self.device_id,
                "sourceType": "WEB",
                "appVersion": "1.0.0",
                "metaDetails": {
                    "userAgent": self.user_agent
                }
            }
        }

        try:
            response = await self.client.post(url, json=payload)
            if response.status_code != 200:
                logging.error(f"Ошибка авторизации (Код {response.status_code}): {response.text}")
                raise Exception(f"HTTP {response.status_code}")

            data = response.json()
            self.token = data.get("token")
            if not self.token:
                raise Exception("Не удалось получить токен авторизации.")
            
            self.client.headers.update({'Authorization': f'Bearer {self.token}'})
            logging.info("✓ Успешная авторизация в Мой Налог.")
            return True
        except Exception as e:
            logging.error(f"Ошибка авторизации в Мой Налог: {e}")
            raise

    async def add_income(self, name, amount, date):
        if not self.token:
            try:
                await self.authenticate()
            except Exception as e:
                logging.error(f"Не удалось авторизоваться: {e}")
                return False

        url = "https://lknpd.nalog.ru/api/v1/income"
        
        if date.tzinfo is None:
            tz = timezone(timedelta(hours=11))
            date = date.replace(tzinfo=tz)
        
        iso_date = date.isoformat(timespec='seconds')
        request_time = datetime.now(date.tzinfo).isoformat(timespec='seconds')
        
        payload = {
            "operationTime": iso_date,
            "requestTime": request_time,
            "services": [
                {
                    "name": name,
                    "amount": amount,
                    "quantity": 1
                }
            ],
            "totalAmount": str(amount),
            "client": {
                "contactPhone": None,
                "displayName": None,
                "inn": None,
                "incomeType": "FROM_INDIVIDUAL"
            },
            "paymentType": "CASH",
            "ignoreMaxTotalIncomeRestriction": False
        }
        
        headers = self.headers.copy()
        headers["Authorization"] = f"Bearer {self.token}"
        
        try:
            response = await self.client.post(url, json=payload, headers=headers)
            
            if response.status_code == 401:
                logging.warning("Токен истек, обновляем...")
                try:
                    await self.authenticate()
                    headers["Authorization"] = f"Bearer {self.token}"
                    response = await self.client.post(url, json=payload, headers=headers)
                except Exception as e:
                    logging.error(f"Ошибка при переавторизации: {e}")
                    return False
            
            if response.status_code == 200:
                logging.info(f"✓ Доход успешно зарегистрирован: {amount} руб. за '{name}'")
                return True
            else:
                logging.error(f"✗ Ошибка регистрации дохода (Код {response.status_code}): {response.text}")
                return False
        except Exception as e:
            logging.error(f"Исключение при регистрации дохода: {e}")
            return False

    async def close(self):
        await self.client.aclose()

class SyncManager:
    def __init__(self):
        try:
            config.validate_config()
        except ValueError as e:
            logging.error(f"Ошибка конфигурации: {e}")
            raise
        
        Configuration.configure(config.YOOKASSA_SHOP_ID, config.YOOKASSA_API_KEY)
        self.nalog = MoyNalogAPI(config.MOY_NALOG_LOGIN, config.MOY_NALOG_PASSWORD)
        self.state_file = f"{LOG_DIR}/sync_state.json"
        self.state = self.load_state()

        # Инициализируем Telegram-нотификатор если настроен
        if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
            thread_id = None
            if config.TELEGRAM_THREAD_ID:
                try:
                    thread_id = int(config.TELEGRAM_THREAD_ID)
                except ValueError:
                    logging.warning(f"TELEGRAM_THREAD_ID имеет некорректное значение: '{config.TELEGRAM_THREAD_ID}'. Сообщения будут отправляться в основной чат.")
            self.notifier = TelegramNotifier(
                bot_token=config.TELEGRAM_BOT_TOKEN,
                chat_id=config.TELEGRAM_CHAT_ID,
                thread_id=thread_id,
            )
            logging.info("✓ Telegram-уведомления включены.")
        else:
            self.notifier = None

    async def startup_notify(self):
        """Вызывается один раз после успешного старта контейнера (не при cron-запусках)."""
        if self.notifier and os.environ.get("TELEGRAM_STARTUP_NOTIFY") == "1":
            await self.notifier.send_startup()

    def load_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r') as f:
                    state = json.load(f)
                if "pending_payments" not in state:
                    state["pending_payments"] = []
                return state
            except:
                pass

        if config.SYNC_START_DATE:
            return {"last_sync_time": f"{config.SYNC_START_DATE}T00:00:00Z", "processed_payments": [], "pending_payments": []}

        return {"last_sync_time": (datetime.now() - timedelta(days=1)).isoformat(), "processed_payments": [], "pending_payments": []}

    def save_state(self):
        with open(self.state_file, 'w') as f:
            json.dump(self.state, f)

    async def get_new_yookassa_payments(self):
        new_payments = []
        last_sync = self.state.get("last_sync_time")
        skip_ids = set(self.state["processed_payments"]) | set(self.state["pending_payments"])

        params = {
            "status": "succeeded",
            "created_at.gte": last_sync
        }

        try:
            res = Payment.list(params)
            for payment in res.items:
                if payment.id not in skip_ids:
                    new_payments.append(payment)

            while res.next_cursor:
                params["cursor"] = res.next_cursor
                res = Payment.list(params)
                for payment in res.items:
                    if payment.id not in skip_ids:
                        new_payments.append(payment)
        except Exception as e:
            logging.error(f"Ошибка ЮKassa: {e}")

        return new_payments

    async def sync(self):
        logging.info("="*60)
        logging.info("Начало синхронизации...")
        logging.info(f"Последняя синхронизация: {self.state.get('last_sync_time')}")

        pending = self.state.get("pending_payments", [])
        if pending:
            logging.warning(f"⚠ Обнаружено {len(pending)} платежей в статусе 'pending' (возможно, были отправлены в налоговую, но статус неизвестен): {pending}")
            logging.warning("Эти платежи пропущены для предотвращения дублей. Проверьте их вручную в ЛК налоговой.")
        
        try:
            new_payments = await self.get_new_yookassa_payments()
            
            if not new_payments:
                logging.info("✓ Новых платежей не найдено.")
                if self.notifier:
                    await self.notifier.send_no_payments()
                return

            logging.info(f"✓ Найдено новых платежей: {len(new_payments)}")

            if self.notifier:
                self.notifier.on_sync_start(len(new_payments))
            
            successful = 0
            failed = 0
            
            for payment in new_payments:
                try:
                    amount = float(payment.amount.value)
                    payment_date = datetime.fromisoformat(payment.created_at.replace('Z', '+00:00'))

                    template_vars = build_template_vars(payment)
                    description = config.INCOME_DESCRIPTION_TEMPLATE.format_map(template_vars)

                    self.state["pending_payments"].append(payment.id)
                    self.save_state()

                    success = await self.nalog.add_income(description, amount, payment_date)

                    self.state["pending_payments"].remove(payment.id)
                    if success:
                        self.state["processed_payments"].append(payment.id)
                        self.state["last_sync_time"] = payment.created_at
                        self.save_state()
                        successful += 1
                        if self.notifier:
                            self.notifier.on_payment_success(amount)
                    else:
                        self.save_state()
                        failed += 1
                        logging.warning(f"Пропуск платежа {payment.id} из-за ошибки. "
                                        f"Платёж заблокирован от повторной отправки для предотвращения дублей.")
                        if self.notifier:
                            self.notifier.on_payment_error(payment.id, "ошибка регистрации дохода")
                except Exception as e:
                    failed += 1
                    logging.error(f"Ошибка при обработке платежа {payment.id}: {e}")
                    if self.notifier:
                        self.notifier.on_payment_error(payment.id, str(e)[:80])

            logging.info(f"Результат: успешно={successful}, ошибок={failed}")
        except Exception as e:
            logging.error(f"Критическая ошибка при синхронизации: {e}", exc_info=True)
        finally:
            await self.nalog.close()
            # Отправляем итоговое сообщение в Telegram
            if self.notifier:
                await self.notifier.send_summary()
            logging.info("Синхронизация завершена.")
            logging.info("="*60)

async def main():
    try:
        manager = SyncManager()
        await manager.startup_notify()
        await manager.sync()
    except Exception as e:
        logging.critical(f"Критическая ошибка: {e}", exc_info=True)
        exit(1)

if __name__ == "__main__":
    asyncio.run(main())
