from datetime import datetime, timedelta, time
from threading import Lock
from time import time
from oauth2client.service_account import ServiceAccountCredentials
import gspread
from concurrent.futures import ThreadPoolExecutor
from cachetools import TTLCache
from retrying import retry
import json

# https://www.youtube.com/watch?v=82DGz7IxW7c -настройка подключения

myscope = ["https://www.googleapis.com/auth/spreadsheets",
           "https://www.googleapis.com/auth/drive"]

# Здесь нужно сменить данные на свои
# Название файла json ключа
creds = ServiceAccountCredentials.from_json_keyfile_name('beautysaloon.json', myscope)
client_main = gspread.authorize(creds)
# Название таблицы
sh = client_main.open('SaloonSheet')
# Страницы таблицы, которые должны игнорироваться во избежание проблем
ignor_worksheets = ['Работники']
# Страница таблицы, на которой перечислены все действующие работники и услуги
name_sheet_workers = 'Работники'
# Названия основных колонок
name_col_service = 'Услуга'
name_col_master = 'Мастер'
#

# Создаем кэш с TTL (временем жизни) в 12 часов
cache_worksheets = TTLCache(maxsize=2, ttl=12 * 60 * 60)
# Создаем кэш с TTL (временем жизни) в 15 минут
cache_days = TTLCache(maxsize=6, ttl=15 * 60)
# Lock для синхронизации доступа к словарям
lock = Lock()


# Функция для сериализации словаря в JSON-строку
def serialize_dict(dct):
    """Сериализатор json"""
    return json.dumps(dct)


# Функция для десериализации JSON-строки в словарь
def deserialize_dict(json_str):
    """Десериализатор json"""
    return json.loads(json_str)


def get_cache_days(service_name: str, master_name: str) -> list or None:
    """
    Запрашивает свободные даты из кэша cashe_days

    :param service_name: Название услуги
    :param master_name: Имя мастера
    """
    if service_name in cache_days:
        cached_value = cache_days[service_name]
        cached_dict = deserialize_dict(cached_value)
        if master_name in cached_dict:
            return cached_dict[master_name]
    return None


def update_cache_days(service_name, master_name, available_dates) -> None:
    """
    Обновляет свободные даты для кэша cache_days

    :param service_name: Название услуги
    :param master_name: Имя мастера
    :param available_dates: Доступные даты
    """
    if master_name is None:
        master_name = 'null'

    if service_name in cache_days:
        cached_value = cache_days[service_name]
        cached_dict = deserialize_dict(cached_value)
        if master_name not in cached_dict:
            cached_dict[master_name] = available_dates
            cache_value = serialize_dict(cached_dict)
            cache_days[service_name] = cache_value
    else:
        cache_value = serialize_dict({master_name: available_dates})
        cache_days[service_name] = cache_value


@retry(wait_exponential_multiplier=3000, wait_exponential_max=3000)
def get_sheet_names() -> list:
    """
    Запрашивает все имена листов таблицы
    """
    # Проверяем, есть ли результат в кэше
    if 'worksheets' in cache_worksheets:
        return cache_worksheets['worksheets']
    with lock:
        worksheets = sh.worksheets()

    # Кэшируем результат
    cache_worksheets['worksheets'] = worksheets
    return worksheets


@retry(wait_exponential_multiplier=3000, wait_exponential_max=3000)
def get_cache_services() -> dict:
    """
    Запрашивает все услуги
    """
    if 'services' in cache_worksheets:
        return cache_worksheets['services']
    dct = {}
    with lock:
        ws = sh.worksheet(name_sheet_workers)
    for i in ws.get_all_records():
        dct[i[name_col_service].strip()] = dct.get(i[name_col_service].strip(), [])
        dct[i[name_col_service].strip()].append(i[name_col_master].strip())

    cache_worksheets['services'] = dct
    return dct


def time_score(func):
    """Декоратор для поиска слабых мест"""

    def wrapper(*args, **kwargs):
        start = time()
        res = func(*args, **kwargs)
        print(f"---{func.__name__} = %s seconds ---" % round(time() - start, 2))
        return res

    return wrapper


class GoogleSheets:
    def __init__(self, client_id):
        self.client_id = client_id
        self.lst_currant_date = None
        self.dct_currant_time = None
        self.lst_records = None

        self.name_service = None
        self.name_master = None
        self.date_record = None
        self.time_record = None

    def __str__(self):
        return f'Инфо о клиенте:' \
               f'{self.client_id=}\n' \
               f'{self.name_service=}\n' \
               f'{self.name_master=}\n' \
               f'{self.date_record=}\n' \
               f'{self.time_record=}'

    @retry(wait_exponential_multiplier=3000, wait_exponential_max=9000)
    def get_all_days(self) -> list:
        """Все доступные дни для записи на определенную услугу"""

        check = get_cache_days(self.name_service, self.name_master)
        if check:
            return check

        @retry(wait_exponential_multiplier=3000, wait_exponential_max=3000)
        def actual_date(sheet_name, count_days=7) -> bool:
            """
            Проверяет по названию листа актуальные даты для записи на ближайшие count_days дней,
            а также наличие свободного времени.

            :param sheet_name: имя листа
            :param count_days: кол-во ближайших дней для поиска
            """
            if sheet_name.title in ignor_worksheets:
                return False
            date_sheet = datetime.strptime(sheet_name.title.strip(), '%d.%m.%y').date()
            date_today = datetime.now()
            if not (date_today.date() <= date_sheet <= (datetime.now().date() + timedelta(days=count_days))):
                return False
            with lock:
                val = sheet_name.get_all_records()
            for dct in val:

                if date_today.date() == date_sheet:
                    if (self.name_master is not None and
                        dct[name_col_master].strip() == self.name_master and
                        dct[name_col_service].strip() == self.name_service) or \
                            (self.name_master is None and
                             dct[name_col_service].strip() == self.name_service):
                        for k, v in dct.items():
                            if str(v).strip() == '' and date_today.time() < datetime.strptime(k, '%H:%M').time():
                                return sheet_name.title
                    continue

                if (self.name_master is not None and dct[name_col_master].strip() == self.name_master and
                    dct[name_col_service].strip() == self.name_service) \
                        or (self.name_master is None and dct[name_col_service].strip() == self.name_service):
                    for k, v in dct.items():
                        if str(v).strip() == '':
                            return sheet_name.title
            return False

        worksheet_all = get_sheet_names()

        with ThreadPoolExecutor(2) as executor:
            res = executor.map(actual_date, worksheet_all)
            res = list(filter(lambda x: type(x) is str, res))

        # Кэшируем результат
        update_cache_days(self.name_service, self.name_master, res)
        return res

    @retry(wait_exponential_multiplier=3000, wait_exponential_max=3000)
    def get_free_time(self) -> list:
        """Функция выгружает ВСЕ СВОБОДНОЕ ВРЕМЯ для определенной ДАТЫ"""

        try:
            with lock:
                all_val = sh.worksheet(self.date_record).get_all_records()
        except gspread.exceptions.WorksheetNotFound as not_found:
            print(not_found, '- Дата занята/не найдена')
            return []

        if self.date_record == datetime.now().strftime('%d.%m.%y'):
            lst = [k.strip() for i in all_val
                   if (self.name_master is None and i[name_col_service].strip() == self.name_service) or
                   (self.name_master is not None and i[name_col_service].strip() == self.name_service and
                    i[name_col_master].strip() == self.name_master)
                   for k, v in i.items() if str(v).strip() == '' and
                   datetime.now().time() < datetime.strptime(k, '%H:%M').time()]
        else:
            lst = [k.strip() for i in all_val
                   if (self.name_master is None and i[name_col_service].strip() == self.name_service) or
                   (self.name_master is not None and i[name_col_service].strip() == self.name_service and
                    i[name_col_master].strip() == self.name_master)
                   for k, v in i.items() if str(v).strip() == '']

        if len(lst) > 0:
            lst = sorted(list(set(lst)))
        return lst

    @retry(wait_exponential_multiplier=3000, wait_exponential_max=3000)
    def set_time(self, client_record='', empty_date='') -> bool:
        """
        Производит в таблицу запись/отмену клиента

        :param client_record: Строка записи клиента в таблицу
        :param empty_date: Строка для заполнения пустая или с определенными данными
        """
        try:
            with lock:
                all_val = sh.worksheet(self.date_record).get_all_records()
        except gspread.exceptions.WorksheetNotFound as not_found:
            print(not_found, '- Дата занята/не найдена')
            return False

        row_num = 1
        for i in all_val:
            row_num += 1
            col_num = 0
            if (self.name_master is None and i[name_col_service].strip() == self.name_service) or \
                    (self.name_master is not None and i[name_col_service].strip() == self.name_service and
                     i[name_col_master].strip() == self.name_master):
                for key_time, val_use in i.items():
                    col_num += 1
                    if key_time.strip() == self.time_record and val_use.strip() == empty_date:
                        if self.name_master is None:
                            self.name_master = i[name_col_master].strip()
                        sh.worksheet(self.date_record).update_cell(row_num, col_num, f'{client_record}')
                        if (self.lst_records and empty_date == '') or (self.lst_records and client_record == ''):
                            record = [self.date_record, self.time_record, self.name_service, self.name_master]
                            if empty_date == '':
                                self.lst_records.append(record)
                            else:
                                self.lst_records.remove(record)
                        return True
        return False

    @retry(wait_exponential_multiplier=3000, wait_exponential_max=3000)
    def get_record(self, client_record: str, count_days=7) -> list:
        """
        Находит все записи клиента на ближайшие <count_days> дней

        :param client_record: строка записи клиента.
        :param count_days: кол-во ближайших дней для поиска.
        :return: list(list) - формата: [Дата, Время, Название услуги, Имя мастера]
        """
        if self.lst_records:
            return self.lst_records

        @retry(wait_exponential_multiplier=3000, wait_exponential_max=3000)
        def check_record(sheet) -> None:
            """Поиск брони клиента"""
            if sheet.title in ignor_worksheets:
                return None
            date_sheet = datetime.strptime(sheet.title, '%d.%m.%y')
            date_today = datetime.now()
            if date_today.date() == date_sheet:
                with lock:
                    all_val = sheet.get_all_records()
                lst_records.extend(
                    [sheet.title.strip(), k.strip(), dct[name_col_service].strip(), dct[name_col_master].strip()]
                    for dct in all_val
                    for k, v in dct.items()
                    if v == client_record and k == date_today.time() < datetime.strptime(k, '%H:%M').time()
                )

            elif date_today.date() < date_sheet.date() <= (date_today + timedelta(days=count_days)).date():
                with lock:
                    all_val = sheet.get_all_records()
                lst_records.extend(
                    [sheet.title.strip(), k.strip(), dct[name_col_service].strip(), dct[name_col_master].strip()]
                    for dct in all_val
                    for k, v in dct.items()
                    if v == client_record
                )

        lst_records = []
        with ThreadPoolExecutor(2) as executor:
            executor.map(check_record, sh.worksheets())
        self.lst_records = lst_records
        return lst_records


# print('GO!')
# a = GoogleSheets(123)
# # a.name_service = 'Маникюр'
# # a.name_master = 'Крапивина Юлия'
# # a.date_record = '29.07.23'
# # a.name_master = 'Королёва Любовь'
# print(a.get_free_time())
