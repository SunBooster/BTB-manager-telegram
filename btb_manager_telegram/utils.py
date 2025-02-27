import configparser
import datetime as dt
import json
import os
import subprocess
from typing import List, Optional

import psutil
import telegram
import yaml

import i18n
from btb_manager_telegram import logger, scheduler, settings
from btb_manager_telegram.error import (
    BTBConfigNotFound,
    NoChatID,
    NoRootPath,
    NoTgConfig,
    TgConfigNotFound,
)


def setup_i18n(lang):
    i18n.set("locale", lang)
    i18n.set("fallback", "en")
    i18n.set("skip_locale_root_data", True)
    i18n.set("filename_format", "{locale}.{format}")
    i18n.load_path.append("./i18n")


def format_float(num):
    return f"{num:0.8f}".rstrip("0").rstrip(".")


def escape_tg(message):
    escape_char = (".", "-", "?", "!", ">", "{", "}", "=", "+", "|")
    escaped_message = ""
    is_escaped = False
    for cur_char in message:
        if cur_char in escape_char and not is_escaped:
            escaped_message += "\\"
        escaped_message += cur_char
        is_escaped = cur_char == "\\" and not is_escaped
    return escaped_message


def reply_text_escape(reply_text_fun):
    def reply_text_escape_fun(message, **kwargs):
        return reply_text_fun(escape_tg(message), **kwargs)

    return reply_text_escape_fun


def setup_root_path_constant():
    if settings.ROOT_PATH is None:
        raise NoRootPath
    else:
        settings.ROOT_PATH = os.path.join(settings.ROOT_PATH, "")


def setup_telegram_constants():
    logger.info("Retrieving Telegram token and chat_id from apprise.yml file.")
    telegram_url = None
    yaml_file_path = os.path.join(settings.ROOT_PATH, "config/apprise.yml")
    if os.path.exists(yaml_file_path):
        with open(yaml_file_path) as f:
            try:
                parsed_urls = yaml.load(f, Loader=yaml.FullLoader)["urls"]
            except Exception as e:
                logger.error(
                    "Unable to correctly read apprise.yml file. Make sure it is correctly set up."
                )
                raise e
            for url in parsed_urls:
                if url.startswith("tgram"):
                    telegram_url = url.split("//")[1]
        if not telegram_url:
            raise NoTgConfig
    else:
        raise TgConfigNotFound

    telegram_url = telegram_url.split("/")
    if len(telegram_url) != 2:
        raise NoChatID

    settings.TOKEN, settings.CHAT_ID = telegram_url
    logger.info(
        f"Successfully retrieved Telegram configuration. "
        f"The bot will only respond to user in the chat with chat_id {settings.CHAT_ID}"
    )


def retreive_btb_constants():
    logger.info("Retreiving binance tokens")
    btb_config_path = os.path.join(settings.ROOT_PATH, "user.cfg")
    if not os.path.isfile(btb_config_path):
        raise BTBConfigNotFound(btb_config_path)
    btb_config = configparser.ConfigParser()
    btb_config.read(btb_config_path)
    settings.BINANCE_API_KEY = btb_config.get("binance_user_config", "api_key")
    settings.BINANCE_API_SECRET = btb_config.get(
        "binance_user_config", "api_secret_key"
    )
    settings.TLD = btb_config.get("binance_user_config", "tld")


def setup_coin_list():
    logger.info("Retreiving coin list")
    coin_list_path = os.path.join(settings.ROOT_PATH, "supported_coin_list")
    with open(coin_list_path, "r") as f:
        coin_list = [line.replace("\n", "").replace(" ", "") for line in f.readlines()]
    settings.COIN_LIST = [i for i in coin_list if i != ""]


def telegram_text_truncator(
    m_list, padding_chars_head="", padding_chars_tail=""
) -> List[str]:
    message = [padding_chars_head]
    index = 0
    for mes in m_list:
        if (
            len(message[index]) + len(mes) + len(padding_chars_tail)
            <= telegram.constants.MAX_MESSAGE_LENGTH
        ):
            message[index] += mes
        else:
            message[index] += padding_chars_tail
            message.append(padding_chars_head + mes)
            index += 1
    message[index] += padding_chars_tail
    return message


def get_binance_trade_bot_process() -> Optional[psutil.Process]:
    name = "binance_trade_bot"
    is_root_path_absolute = os.path.isabs(settings.ROOT_PATH)
    bot_path = os.path.normpath(settings.ROOT_PATH)
    if not is_root_path_absolute:
        bot_path = os.path.normpath(os.path.join(os.getcwd(), settings.ROOT_PATH))

    for proc in psutil.process_iter():
        try:
            if (
                name in proc.name() or name in " ".join(proc.cmdline())
            ) and proc.cwd() == bot_path:
                return proc
        except psutil.AccessDenied:
            continue
        except psutil.ZombieProcess:
            continue


def find_and_kill_binance_trade_bot_process():
    try:
        binance_trade_bot_process = get_binance_trade_bot_process()
        binance_trade_bot_process.terminate()
        binance_trade_bot_process.wait()
    except Exception as e:
        logger.info(f"ERROR: {e}")


def kill_btb_manager_telegram_process():
    try:
        btb_manager_telegram_pid = os.getpid()
        btb_manager_telegram_process = psutil.Process(btb_manager_telegram_pid)
        btb_manager_telegram_process.kill()
        btb_manager_telegram_process.wait()
    except Exception as e:
        logger.info(f"ERROR: {e}")


def is_tg_bot_update_available():
    try:
        proc = subprocess.Popen(
            ["bash", "-c", "git remote update origin && git status -uno"],
            stdout=subprocess.PIPE,
        )
        output, _ = proc.communicate()
        re = "Your branch is behind" in str(output)
    except Exception as e:
        logger.error(e, exc_info=True)
        re = None
    return re


def is_btb_bot_update_available():
    try:
        subprocess.run(["git", "remote", "update", "origin"])
        current_version = (
            subprocess.check_output(["git", "describe", "--abbrev=0", "--tags"])
            .decode()
            .rstrip("\n")
        )
        remote_version = (
            subprocess.check_output(
                ["git", "describe", "--abbrev=0", "--tags", "origin/main"]
            )
            .decode()
            .rstrip("\n")
        )
        re = current_version != remote_version
    except Exception as e:
        logger.error(e, exc_info=True)
        re = None
    return re


def update_checker():
    logger.info("Checking for updates.")

    if settings.TG_UPDATE_BROADCASTED_BEFORE is False:
        if is_tg_bot_update_available():
            logger.info("BTB Manager Telegram update found.")
            message = (
                f"{i18n.t('update.tgb.available')}\n\n"
                f"{i18n.t('update.tgb.instruction')}"
            )
            settings.TG_UPDATE_BROADCASTED_BEFORE = True
            settings.CHAT.send_message(escape_tg(message), parse_mode="MarkdownV2")
            scheduler.enter(
                dt.timedelta(days=7).total_seconds(),
                1,
                update_reminder,
                ("_*Reminder*_:\n\n" + message,),
            )

    if settings.BTB_UPDATE_BROADCASTED_BEFORE is False:
        if is_btb_bot_update_available():
            logger.info("Binance Trade Bot update found.")
            message = (
                f"{i18n.t('update.btb.available')}\n\n"
                f"{i18n.t('update.btb.instruction')}"
            )
            settings.BTB_UPDATE_BROADCASTED_BEFORE = True
            settings.CHAT.send_message(escape_tg(message), parse_mode="MarkdownV2")
            scheduler.enter(
                dt.timedelta(days=7).total_seconds(),
                1,
                update_reminder,
                ("_*Reminder*_:\n\n" + message,),
            )

    if (
        settings.TG_UPDATE_BROADCASTED_BEFORE is False
        or settings.BTB_UPDATE_BROADCASTED_BEFORE is False
    ):
        scheduler.enter(
            dt.timedelta(days=1).total_seconds(),
            1,
            update_checker,
        )


def update_reminder(self, message):
    logger.info(f"Reminding user: {message}")
    settings.CHAT.send_message(escape_tg(message), parse_mode="MarkdownV2")
    scheduler.enter(
        dt.timedelta(days=7).total_seconds(),
        1,
        update_reminder,
        ("_*Reminder*_:\n\n" + message,),
    )


def get_custom_scripts_keyboard():
    logger.info("Getting list of custom scripts.")

    custom_scripts_path = "./config/custom_scripts.json"
    keyboard = []
    custom_script_exist = False
    message = i18n.t("script.no_script")

    if os.path.exists(custom_scripts_path):
        with open(custom_scripts_path) as f:
            scripts = json.load(f)
            for script_name in scripts:
                keyboard.append([script_name])

        if len(keyboard) >= 1:
            custom_script_exist = True
            message = i18n.t("script.select")
    else:
        logger.warning(
            "Unable to find custom_scripts.json file inside BTB-manager-telegram's config/ directory."
        )
        message = i18n.t("script.no_config")

    keyboard.append([i18n.t("keyboard.cancel")])
    return keyboard, custom_script_exist, message
