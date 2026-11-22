from email_assistant.config import ConfigurationError, Role, load_settings


def main() -> None:
    try:
        load_settings(Role.JOB)
    except ConfigurationError as exc:
        raise SystemExit(str(exc)) from None
    print("MailPilot Job smoke check passed.")
