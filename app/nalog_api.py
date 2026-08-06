import logging
import httpx
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential
import config
from utils import generate_device_id_from_login


class TransientNalogError(RuntimeError):
    pass


class PermanentNalogError(RuntimeError):
    pass


AUTH_RETRY = retry_if_exception_type((httpx.RequestError, TransientNalogError))


class MoyNalogAPI:
    def __init__(self, login, password, auth_method="password", refresh_token=None, on_refresh_token=None):
        self.login = login
        self.password = password
        self.auth_method = auth_method
        self.refresh_token = refresh_token
        self.on_refresh_token = on_refresh_token
        self.token = None
        self.last_error = None
        self.last_error_kind = None
        self.last_error_retryable = False
        self.last_operation_uncertain = False

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

    def _reset_error(self):
        self.last_error = None
        self.last_error_kind = None
        self.last_error_retryable = False
        self.last_operation_uncertain = False

    def _extract_error(self, response):
        try:
            data = response.json()
            message = data.get("message") if isinstance(data, dict) else None
            if message:
                return str(message)[:200]
        except Exception:
            pass
        content_type = response.headers.get("content-type", "").lower()
        if "html" in content_type:
            return f"ФНС вернула HTML вместо API-ответа (HTTP {response.status_code}, возможны техработы)"
        return f"HTTP {response.status_code}"

    def _record_http_error(self, response, *, write_attempted=False):
        status = response.status_code
        transient = status in (408, 425, 429) or 500 <= status <= 599
        if status == 429:
            kind = "rate_limited"
            message = "ФНС временно ограничила запросы (HTTP 429)"
        elif status in (502, 503, 504):
            kind = "maintenance"
            message = f"ФНС временно недоступна (HTTP {status})"
        elif status in (401, 403):
            kind = "auth"
            message = f"ФНС отклонила авторизацию (HTTP {status})"
        elif status >= 500:
            kind = "server"
            message = f"внутренняя ошибка ФНС (HTTP {status})"
        else:
            kind = "http"
            message = self._extract_error(response)
        self.last_error = message
        self.last_error_kind = kind
        self.last_error_retryable = transient
        self.last_operation_uncertain = write_attempted and (
            status == 408 or status >= 500
        )

    def _record_exception(self, exc, *, write_attempted=False):
        if isinstance(exc, (httpx.ConnectTimeout, httpx.PoolTimeout)):
            kind = "connect_timeout"
            message = "не удалось установить соединение с ФНС вовремя"
            uncertain = False
        elif isinstance(exc, httpx.ConnectError):
            kind = "connection"
            message = "не удалось подключиться к ФНС"
            uncertain = False
        elif isinstance(exc, httpx.TimeoutException):
            kind = "timeout"
            message = "ФНС не ответила вовремя"
            uncertain = write_attempted
        elif isinstance(exc, (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError)):
            kind = "connection_reset"
            message = "соединение с ФНС было прервано"
            uncertain = write_attempted
        elif isinstance(exc, (ValueError, TypeError)):
            kind = "bad_response"
            message = "ФНС вернула некорректный ответ (возможны техработы)"
            uncertain = write_attempted
        else:
            kind = "unexpected"
            message = f"сбой ФНС ({type(exc).__name__})"
            uncertain = write_attempted
        self.last_error = message
        self.last_error_kind = kind
        self.last_error_retryable = kind not in ("unexpected",)
        self.last_operation_uncertain = uncertain

    async def authenticate(self):
        self._reset_error()
        if self.auth_method == "refresh":
            return await self._authenticate_refresh()
        return await self._authenticate_password()

    @retry(
        retry=AUTH_RETRY,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
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
                self._record_http_error(response)
                logging.error(f"Ошибка авторизации: {self.last_error}")
                error_type = (
                    TransientNalogError
                    if self.last_error_retryable
                    else PermanentNalogError
                )
                raise error_type(self.last_error)

            try:
                data = response.json()
            except Exception as e:
                self._record_exception(e)
                raise TransientNalogError(self.last_error) from e
            self.token = data.get("token")
            if not self.token:
                self._record_exception(ValueError("missing token"))
                raise TransientNalogError(self.last_error)

            self._reset_error()
            self.client.headers.update({'Authorization': f'Bearer {self.token}'})
            logging.info("✓ Успешная авторизация в Мой Налог.")
            return True
        except Exception as e:
            if self.last_error is None:
                self._record_exception(e)
            logging.error(f"Ошибка авторизации в Мой Налог: {e}")
            raise

    @retry(
        retry=AUTH_RETRY,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
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
                self._record_http_error(response)
                logging.error(f"Ошибка авторизации по refresh token: {self.last_error}")
                error_type = (
                    TransientNalogError
                    if self.last_error_retryable
                    else PermanentNalogError
                )
                raise error_type(self.last_error)

            try:
                data = response.json()
            except Exception as e:
                self._record_exception(e)
                raise TransientNalogError(self.last_error) from e
            self.token = data.get("token")
            if not self.token:
                self._record_exception(ValueError("missing token"))
                raise TransientNalogError(self.last_error)

            new_refresh = data.get("refreshToken")
            if new_refresh and new_refresh != self.refresh_token:
                self.refresh_token = new_refresh
                logging.info("Получен обновлённый refreshToken, сохраняем в state.")
                if self.on_refresh_token:
                    self.on_refresh_token(new_refresh)

            self._reset_error()
            self.client.headers.update({'Authorization': f'Bearer {self.token}'})
            logging.info("✓ Успешная авторизация в Мой Налог (refresh token).")
            return True
        except Exception as e:
            if self.last_error is None:
                self._record_exception(e)
            logging.error(f"Ошибка авторизации в Мой Налог по refresh token: {e}")
            raise

    async def add_income(self, name, amount, date):
        self._reset_error()
        try:
            amount = Decimal(str(amount)).quantize(Decimal("0.01"))
        except InvalidOperation:
            self.last_error = "некорректная сумма дохода"
            return None
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
                    "amount": str(amount),
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

        write_attempted = False
        try:
            write_attempted = True
            response = await self.client.post(url, json=payload, headers=headers)

            if response.status_code == 401:
                logging.warning("Токен истек, обновляем...")
                try:
                    await self.authenticate()
                    headers["Authorization"] = f"Bearer {self.token}"
                    write_attempted = True
                    response = await self.client.post(url, json=payload, headers=headers)
                except Exception as e:
                    logging.error(f"Ошибка при переавторизации: {e}")
                    return None

            if response.status_code == 200:
                try:
                    data = response.json()
                except Exception as e:
                    self._record_exception(e, write_attempted=True)
                    logging.error(self.last_error)
                    return None
                receipt_uuid = data.get("approvedReceiptUuid")
                if not receipt_uuid:
                    self._record_exception(
                        ValueError("missing approvedReceiptUuid"),
                        write_attempted=True,
                    )
                    logging.error(self.last_error)
                    return None
                logging.info(f"✓ Доход успешно зарегистрирован: {amount} руб. за '{name}' (чек: {receipt_uuid})")
                return receipt_uuid
            else:
                self._record_http_error(response, write_attempted=True)
                logging.error(f"✗ Ошибка регистрации дохода: {self.last_error}")
                return None
        except Exception as e:
            if self.last_error is None:
                self._record_exception(e, write_attempted=write_attempted)
            logging.error(f"Исключение при регистрации дохода: {self.last_error}")
            return None

    async def cancel_income(self, receipt_uuid):
        self._reset_error()
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

        write_attempted = False
        try:
            write_attempted = True
            response = await self.client.post(url, json=payload, headers=headers)

            if response.status_code == 401:
                logging.warning("Токен истек, обновляем...")
                try:
                    await self.authenticate()
                    headers["Authorization"] = f"Bearer {self.token}"
                    write_attempted = True
                    response = await self.client.post(url, json=payload, headers=headers)
                except Exception as e:
                    logging.error(f"Ошибка при переавторизации: {e}")
                    return False

            if response.status_code == 200:
                logging.info(f"✓ Чек {receipt_uuid} успешно аннулирован (возврат средств)")
                return True
            else:
                self._record_http_error(response, write_attempted=True)
                logging.error(f"✗ Ошибка аннулирования чека: {self.last_error}")
                return False
        except Exception as e:
            if self.last_error is None:
                self._record_exception(e, write_attempted=write_attempted)
            logging.error(f"Исключение при аннулировании чека: {self.last_error}")
            return False

    async def find_income(self, name, amount, operation_date=None):
        self._reset_error()
        if not self.token:
            try:
                await self.authenticate()
            except Exception as e:
                logging.error(f"Не удалось авторизоваться для проверки чеков: {e}")
                return None

        if operation_date:
            center = operation_date.astimezone()
            from_date = (center - timedelta(days=1)).isoformat(timespec='milliseconds')
            to_date = (center + timedelta(days=1)).isoformat(timespec='milliseconds')
        else:
            now = datetime.now().astimezone()
            from_date = (now - timedelta(days=7)).isoformat(timespec='milliseconds')
            to_date = now.isoformat(timespec='milliseconds')

        url = "https://lknpd.nalog.ru/api/v1/incomes"
        params = {
            "from": from_date,
            "to": to_date,
            "offset": 0,
            "sortBy": "operation_time:desc",
            "limit": 50,
        }

        headers = self.headers.copy()
        headers["Authorization"] = f"Bearer {self.token}"

        try:
            seen_pages = set()
            for _ in range(100):
                response = await self.client.get(url, params=params, headers=headers)

                if response.status_code == 401:
                    await self.authenticate()
                    headers["Authorization"] = f"Bearer {self.token}"
                    response = await self.client.get(
                        url, params=params, headers=headers
                    )

                try:
                    if response.status_code != 200:
                        self._record_http_error(response)
                        logging.error(
                            f"Ошибка получения списка чеков: {self.last_error}"
                        )
                        return None
                    data = response.json()
                except Exception as e:
                    self._record_exception(e)
                    logging.error(self.last_error)
                    return None

                incomes = data.get("content", [])
                page_marker = tuple(
                    item.get("approvedReceiptUuid") or item.get("receiptUuid")
                    for item in incomes
                )
                if page_marker in seen_pages:
                    logging.warning(
                        "ФНС повторила страницу списка чеков; поиск остановлен."
                    )
                    return None
                seen_pages.add(page_marker)

                for income in incomes:
                    if income.get("cancellationInfo"):
                        continue
                    try:
                        income_amount = Decimal(
                            str(income.get("totalAmount", 0))
                        )
                        expected_amount = Decimal(str(amount))
                    except InvalidOperation:
                        continue
                    if (
                        income.get("name") == name
                        and income_amount == expected_amount
                    ):
                        receipt_uuid = income.get("approvedReceiptUuid")
                        logging.info(
                            "✓ Чек найден в налоговой при верификации: "
                            f"{receipt_uuid}"
                        )
                        return receipt_uuid

                if len(incomes) < params["limit"]:
                    return None
                params["offset"] += params["limit"]
            logging.warning("Достигнут предел страниц при поиске чека в ФНС.")
        except Exception as e:
            if self.last_error is None:
                self._record_exception(e)
            logging.error(f"Исключение при проверке чеков: {self.last_error}")

        return None

    async def get_income_status(self, receipt_uuid, operation_date=None):
        """Вернуть active/cancelled/not_found/error без повторной записи в ФНС."""
        self._reset_error()
        if not self.token:
            try:
                await self.authenticate()
            except Exception as e:
                logging.error(f"Не удалось авторизоваться для сверки чека: {e}")
                return "error"

        center = operation_date.astimezone() if operation_date else datetime.now().astimezone()
        url = "https://lknpd.nalog.ru/api/v1/incomes"
        params = {
            "from": (center - timedelta(days=1)).isoformat(timespec="milliseconds"),
            "to": (center + timedelta(days=1)).isoformat(timespec="milliseconds"),
            "offset": 0,
            "sortBy": "operation_time:desc",
            "limit": 50,
        }
        headers = self.headers.copy()
        headers["Authorization"] = f"Bearer {self.token}"

        try:
            seen_pages = set()
            for _ in range(100):
                response = await self.client.get(url, params=params, headers=headers)
                if response.status_code == 401:
                    await self.authenticate()
                    headers["Authorization"] = f"Bearer {self.token}"
                    response = await self.client.get(url, params=params, headers=headers)
                if response.status_code != 200:
                    self._record_http_error(response)
                    return "error"
                try:
                    incomes = response.json().get("content", [])
                except Exception as e:
                    self._record_exception(e)
                    return "error"
                marker = tuple(
                    item.get("approvedReceiptUuid") or item.get("receiptUuid")
                    for item in incomes
                )
                if marker in seen_pages:
                    self.last_error = "ФНС повторила страницу списка чеков"
                    self.last_error_kind = "bad_response"
                    self.last_error_retryable = True
                    return "error"
                seen_pages.add(marker)
                for income in incomes:
                    income_uuid = income.get("approvedReceiptUuid") or income.get("receiptUuid")
                    if income_uuid == receipt_uuid:
                        return "cancelled" if income.get("cancellationInfo") else "active"
                if len(incomes) < params["limit"]:
                    return "not_found"
                params["offset"] += params["limit"]
        except Exception as e:
            if self.last_error is None:
                self._record_exception(e)
            logging.error(f"Исключение при сверке статуса чека: {self.last_error}")
            return "error"
        self.last_error = "достигнут предел страниц при сверке чека"
        self.last_error_kind = "bad_response"
        self.last_error_retryable = True
        return "error"

    async def close(self):
        await self.client.aclose()
