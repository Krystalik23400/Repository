import getpass
import json
import os
import re
import socket
import ssl
import sys
import time
from decimal import Decimal, InvalidOperation
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEX_API = "https://api.dexscreener.com"
HELP = (
    "Пришли название, тикер или торговую пару, например /coin SOL или SOL/USDC.\n"
    "Для поиска по адресу укажи сеть и адрес токена:\n"
    "/address solana So11111111111111111111111111111111111111112"
)


class ApiError(Exception):
    def __init__(self, status=None, retry_after=0, *, detail=None):
        super().__init__(detail or (f"HTTP {status}" if status else "Ошибка сети или формата ответа"))
        self.status = status
        self.retry_after = retry_after


def request_json(url, payload=None, timeout=10):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    try:
        with urlopen(Request(url, data=data, headers=headers), timeout=timeout) as response:
            return json.load(response, parse_float=Decimal)
    except HTTPError as error:
        with error:
            if error.code == 429:
                try:
                    retry = json.load(error).get("parameters", {}).get("retry_after", 0)
                    if type(retry) is int and retry > 0:
                        raise ApiError(429, retry) from None
                except (ValueError, AttributeError, OSError, HTTPException):
                    pass
            raise ApiError(error.code) from None
    except (OSError, HTTPException, ValueError) as error:
        reason = error.reason if isinstance(error, URLError) else error
        if isinstance(reason, TimeoutError):
            detail = "Истекло время ожидания ответа"
        elif isinstance(reason, ssl.SSLError):
            detail = "Ошибка защищённого соединения (TLS)"
        elif isinstance(reason, socket.gaierror):
            detail = "Не удалось найти сервер (DNS)"
        elif isinstance(reason, ValueError):
            detail = "Сервер вернул некорректный JSON"
        else:
            proxy = re.match(r"Tunnel connection failed: (\d{3})\b", str(reason))
            detail = f"Прокси отклонил соединение (HTTP {proxy[1]})" if proxy else "Ошибка сети"
        raise ApiError(detail=detail) from None


def object_or_empty(value):
    return value if isinstance(value, dict) else {}


def number(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


def usd(value, price=False):
    amount = number(value)
    if amount is None or amount < 0:
        return "—"
    if amount >= Decimal("1e15") or 0 < amount < Decimal("1e-12"):
        return f"${amount:.2e}"
    digits = max(2, min(12, 2 - amount.adjusted())) if price and amount else 2
    return f"${amount:,.{digits}f}"


def label(value, limit=55):
    if not isinstance(value, str):
        return "—"
    return " ".join(value.split())[:limit] or "—"


def rank_pair(pair, query, by_address):
    base = object_or_empty(pair.get("baseToken"))
    quote = object_or_empty(pair.get("quoteToken"))
    if by_address:
        # Hex addresses are case-insensitive; addresses such as Solana's are not.
        addresses = (base.get("address"), quote.get("address"))
        if re.fullmatch(r"0x[0-9a-f]{40}", query, re.IGNORECASE):
            addresses = tuple(str(address).lower() for address in addresses)
            query = query.lower()
        relevance = 2 if addresses[0] == query else int(addresses[1] == query)
    else:
        base_names = {str(base.get(key, "")).casefold() for key in ("name", "symbol")}
        quote_names = {str(quote.get(key, "")).casefold() for key in ("name", "symbol")}
        relevance = 2 if query.casefold() in base_names else int(query.casefold() in quote_names)
    liquidity = number(object_or_empty(pair.get("liquidity")).get("usd"))
    return relevance, liquidity if liquidity is not None else Decimal("-1")


def format_pair(pair):
    base = object_or_empty(pair.get("baseToken"))
    quote = object_or_empty(pair.get("quoteToken"))
    base_symbol = label(base.get("symbol"))
    change = number(object_or_empty(pair.get("priceChange")).get("h24"))
    change_text = "—" if change is None else f"{change:+.3g}%"
    lines = [
        f"{base_symbol} / {label(quote.get('symbol'))} · {label(pair.get('dexId'))}",
        f"Сеть {label(pair.get('chainId'))} · {label(base.get('name'))}",
        f"Цена 1 {base_symbol}  {usd(pair.get('priceUsd'), price=True)}",
        f"Изменение за 24 ч  {change_text}",
        f"Объём за 24 ч  {usd(object_or_empty(pair.get('volume')).get('h24'))}",
        f"Ликвидность  {usd(object_or_empty(pair.get('liquidity')).get('usd'))}",
    ]
    url = pair.get("url")
    if isinstance(url, str) and re.fullmatch(
        r"https://(?:dexscreener\.com/[\w-]+/|www\.geckoterminal\.com/[\w-]+/pools/)[\w:.-]+",
        url,
        re.ASCII,
    ):
        lines.append(url)
    return "\n".join(lines)


def geckoterminal_pairs(query, chain=None):
    params = {"include": "base_token,quote_token,dex"}
    symbols = []
    if chain is None:
        endpoint = "/search/pools"
        base_query, _, quote_query = query.partition("/")
        if base_query.strip() and quote_query.strip():
            symbols = sorted((base_query.strip().casefold(), quote_query.strip().casefold()))
        params["query"] = base_query.strip() if symbols else query
    else:
        network = {"ethereum": "eth", "polygon": "polygon_pos", "avalanche": "avax"}.get(chain, chain)
        endpoint = f"/networks/{network}/tokens/{query}/pools"

    url = "https://api.geckoterminal.com/api/v2" + endpoint + "?" + urlencode(params)
    result = object_or_empty(request_json(url))
    if not isinstance(result.get("data"), list):
        raise ValueError("Ответ API имеет неожиданный формат")

    included = result.get("included")
    resources = {
        item["id"]: object_or_empty(item.get("attributes"))
        for item in (included if isinstance(included, list) else [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    pairs = []
    for pool in result["data"]:
        pool = object_or_empty(pool)
        attributes = object_or_empty(pool.get("attributes"))
        relationships = object_or_empty(pool.get("relationships"))
        address = attributes.get("address")
        pool_id = pool.get("id")
        if not isinstance(address, str) or not isinstance(pool_id, str):
            continue
        suffix = "_" + address
        if not pool_id.endswith(suffix):
            continue
        network = pool_id.removesuffix(suffix)
        def reference(role):
            data = object_or_empty(object_or_empty(relationships.get(role)).get("data"))
            resource_id = data.get("id")
            return resource_id if isinstance(resource_id, str) else ""

        base = resources.get(reference("base_token"), {})
        quote = resources.get(reference("quote_token"), {})
        dex_id = reference("dex")
        if symbols:
            pool_symbols = sorted(str(token.get("symbol", "")).casefold() for token in (base, quote))
            if symbols != pool_symbols:
                continue
        pairs.append({
            "chainId": network,
            "dexId": resources.get(dex_id, {}).get("name") or dex_id,
            "baseToken": base,
            "quoteToken": quote,
            "priceUsd": attributes.get("base_token_price_usd"),
            "priceChange": attributes.get("price_change_percentage"),
            "volume": attributes.get("volume_usd"),
            "liquidity": {"usd": attributes.get("reserve_in_usd")},
            "url": f"https://www.geckoterminal.com/{network}/pools/{address}",
        })
    return pairs


def reply_for(text):
    parts = text.strip().split(maxsplit=1)
    if not parts:
        return HELP
    command = parts[0].lower()
    argument = parts[1] if len(parts) > 1 else ""
    if command in ("/start", "/help"):
        return HELP
    by_address = command == "/address"
    chain = None
    if by_address:
        address_parts = argument.split()
        if len(address_parts) != 2:
            return HELP
        chain, query = address_parts
        if not re.fullmatch(r"[a-z0-9-]{1,40}", chain.lower()) or not re.fullmatch(
            r"[A-Za-z0-9_:+.-]{2,150}", query
        ) or query in (".", ".."):
            return HELP
        chain = chain.lower()
        url = f"{DEX_API}/token-pairs/v1/{chain}/{query}"
    else:
        if command.startswith("/") and command != "/coin":
            return HELP
        query = argument if command == "/coin" else text.strip()
        if not 1 <= len(query) <= 150:
            return HELP
        url = f"{DEX_API}/latest/dex/search?" + urlencode({"q": query})

    source = "DEX Screener"
    try:
        result = request_json(url)
        if by_address:
            pairs = result
        elif isinstance(result, dict) and ("pairs" in result or "schemaVersion" in result):
            pairs = result.get("pairs")
            if pairs is None:
                pairs = []
        else:
            pairs = None
        if not isinstance(pairs, list):
            return "DEX Screener вернул ответ в неожиданном формате."
    except ApiError as error:
        if error.status == 403:
            source = "GeckoTerminal"
            try:
                pairs = geckoterminal_pairs(query, chain)
            except (ApiError, ValueError) as fallback_error:
                if isinstance(fallback_error, ApiError) and fallback_error.status == 429:
                    return "GeckoTerminal ограничил частоту запросов. Попробуй через минуту."
                return f"DEX Screener вернул HTTP 403. GeckoTerminal тоже недоступен. {fallback_error}."
        elif error.status == 429:
            return "DEX Screener ограничил частоту запросов. Попробуй через минуту."
        else:
            return f"Не удалось получить данные от DEX Screener. {error}."

    pairs = [pair for pair in pairs if isinstance(pair, dict)]
    if not pairs:
        return "Торговые пары не найдены. Проверь название, сеть или адрес токена."
    pairs.sort(key=lambda pair: rank_pair(pair, query, by_address), reverse=True)
    return f"Данные {source}\n\n" + "\n\n".join(format_pair(pair) for pair in pairs[:3])


def run(token):
    def telegram(method, payload=None, timeout=10):
        result = request_json(f"https://api.telegram.org/bot{token}/{method}", payload, timeout)
        if not isinstance(result, dict) or result.get("ok") is not True or "result" not in result:
            raise ApiError()
        return result["result"]

    print("Подключаюсь к Telegram…", flush=True)
    bot = telegram("getMe")
    print(f"Бот запущен. Открой https://t.me/{bot['username']} и отправь /start.", flush=True)
    offset = None
    delay = 1
    while True:
        settings = {"timeout": 25, "allowed_updates": ["message"]}
        if offset is not None:
            settings["offset"] = offset
        try:
            updates = telegram("getUpdates", settings, timeout=35)
        except ApiError as error:
            if error.status in (401, 404):
                raise
            if error.status == 409:
                sys.exit("Telegram вернул HTTP 409. Останови второй экземпляр бота или отключи webhook.")
            print(f"Telegram: {error}. Повторяю подключение.", file=sys.stderr, flush=True)
            time.sleep(error.retry_after or delay)
            delay = min(delay * 2, 30)
            continue
        delay = 1
        if not updates:
            offset = None
        for update in updates:
            message = update.get("message", {})
            chat = message.get("chat", {})
            if chat.get("type") == "private" and isinstance(message.get("text"), str):
                response = {
                    "chat_id": chat["id"],
                    "text": reply_for(message["text"]),
                    "link_preview_options": {"is_disabled": True},
                }
                try:
                    telegram("sendMessage", response)
                except ApiError as error:
                    if error.status == 429:
                        time.sleep(error.retry_after or 1)
                        try:
                            telegram("sendMessage", response)
                        except ApiError as retry_error:
                            print(f"Ответ Telegram не отправлен: {retry_error}.", file=sys.stderr)
                    else:
                        print(f"Ответ Telegram не отправлен: {error}.", file=sys.stderr)
            offset = update["update_id"] + 1


if __name__ == "__main__":
    try:
        token = os.getenv("TELEGRAM_BOT_TOKEN") or getpass.getpass("Токен Telegram-бота: ")
        token = token.strip()
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
            sys.exit("Проверь токен, выданный @BotFather.")
        run(token)
    except ApiError as error:
        if error.status in (401, 404):
            sys.exit("Telegram отклонил токен. Скопируй его заново из @BotFather.")
        sys.exit(f"Не удалось подключиться к Telegram: {error}.")
    except EOFError:
        sys.exit("Введи токен в терминале или задай TELEGRAM_BOT_TOKEN.")
    except KeyboardInterrupt:
        print("\nБот остановлен.")
