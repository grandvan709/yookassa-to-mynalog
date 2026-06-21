import logging
import httpx
from datetime import datetime, timedelta
from tenacity import retry, stop_after_attempt, wait_exponential
import config
from utils import generate_device_id_from_login


class MoyNalogAPI:
    def __init__(self, login, password, auth_method="password", refresh_token=None, on_refresh_token=None):
        self.login = login
        self.password = password
        self.auth_method = auth_method
        self.refresh_token = refresh_token
        self.on_refresh_token = on_refresh_token
        self.token = None

        logging.info(f"Метод авторизации Мой Налог: {auth_method}")

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

    async def authenticate(self):
        if self.auth_method == "refresh":
            return await self._authenticate_refresh()
        return await self._authenticate_password()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def _authenticate_password(self):
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

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def _authenticate_refresh(self):
        url = "https://lknpd.nalog.ru/api/v1/auth/token"
        payload = {
            "deviceInfo": {
                "sourceDeviceId": self.device_id,
                "sourceType": "WEB",
                "appVersion": "1.0.0",
                "metaDetails": {
                    "userAgent": self.user_agent
                }
            },
            "refreshToken": self.refresh_token
        }

        try:
            response = await self.client.post(url, json=payload)
            if response.status_code != 200:
                logging.error(f"Ошибка авторизации по refresh token (Код {response.status_code}): {response.text}")
                raise Exception(f"HTTP {response.status_code}")

            data = response.json()
            self.token = data.get("token")
            if not self.token:
                raise Exception("Не удалось получить токен авторизации по refresh token.")

            new_refresh = data.get("refreshToken")
            if new_refresh and new_refresh != self.refresh_token:
                self.refresh_token = new_refresh
                logging.info("Получен обновлённый refreshToken, сохраняем в state.")
                if self.on_refresh_token:
                    self.on_refresh_token(new_refresh)

            self.client.headers.update({'Authorization': f'Bearer {self.token}'})
            logging.info("✓ Успешная авторизация в Мой Налог (refresh token).")
            return True
        except Exception as e:
            logging.error(f"Ошибка авторизации в Мой Налог по refresh token: {e}")
            raise

    async def add_income(self, name, amount, date):
        if not self.token:
            try:
                await self.authenticate()
            except Exception as e:
                logging.error(f"Не удалось авторизоваться: {e}")
                return None

        url = "https://lknpd.nalog.ru/api/v1/income"

        date = date.astimezone()
        iso_date = date.isoformat(timespec='seconds')
        request_time = datetime.now().astimezone().isoformat(timespec='seconds')

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
                    return None

            if response.status_code == 200:
                data = response.json()
                receipt_uuid = data.get("approvedReceiptUuid")
                logging.info(f"✓ Доход успешно зарегистрирован: {amount} руб. за '{name}' (чек: {receipt_uuid})")
                return receipt_uuid
            else:
                logging.error(f"✗ Ошибка регистрации дохода (Код {response.status_code}): {response.text}")
                return None
        except Exception as e:
            logging.error(f"Исключение при регистрации дохода: {e}")
            return None

    async def cancel_income(self, receipt_uuid):
        if not self.token:
            try:
                await self.authenticate()
            except Exception as e:
                logging.error(f"Не удалось авторизоваться: {e}")
                return False

        url = "https://lknpd.nalog.ru/api/v1/cancel"

        now = datetime.now().astimezone()
        iso_now = now.isoformat(timespec='seconds')

        payload = {
            "operationTime": iso_now,
            "requestTime": iso_now,
            "comment": "Возврат средств",
            "receiptUuid": receipt_uuid
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
                logging.info(f"✓ Чек {receipt_uuid} успешно аннулирован (возврат средств)")
                return True
            else:
                logging.error(f"✗ Ошибка аннулирования чека {receipt_uuid} (Код {response.status_code}): {response.text}")
                return False
        except Exception as e:
            logging.error(f"Исключение при аннулировании чека: {e}")
            return False

    async def find_income(self, name, amount):
        if not self.token:
            try:
                await self.authenticate()
            except Exception as e:
                logging.error(f"Не удалось авторизоваться для проверки чеков: {e}")
                return None

        now = datetime.now().astimezone()
        from_date = (now - timedelta(days=7)).isoformat(timespec='milliseconds')
        to_date = now.isoformat(timespec='milliseconds')

        url = f"https://lknpd.nalog.ru/api/v1/incomes?from={from_date}&to={to_date}&offset=0&sortBy=operation_time:desc&limit=50"

        headers = self.headers.copy()
        headers["Authorization"] = f"Bearer {self.token}"

        try:
            response = await self.client.get(url, headers=headers)

            if response.status_code == 401:
                await self.authenticate()
                headers["Authorization"] = f"Bearer {self.token}"
                response = await self.client.get(url, headers=headers)

            if response.status_code != 200:
                logging.error(f"Ошибка получения списка чеков (Код {response.status_code}): {response.text}")
                return None

            data = response.json()
            for income in data.get("content", []):
                if income.get("cancellationInfo"):
                    continue
                if income.get("name") == name and float(income.get("totalAmount", 0)) == float(amount):
                    receipt_uuid = income.get("approvedReceiptUuid")
                    logging.info(f"✓ Чек найден в налоговой при верификации: {receipt_uuid}")
                    return receipt_uuid
        except Exception as e:
            logging.error(f"Исключение при проверке чеков: {e}")

        return None

    async def close(self):
        await self.client.aclose()
