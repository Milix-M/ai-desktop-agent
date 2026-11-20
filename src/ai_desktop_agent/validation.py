"""Validation for text that must remain safe to serialize into task history."""


def validate_text(value: object, name: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{name}は文字列で指定してください")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(f"{name}に不正なUnicodeが含まれています") from error
