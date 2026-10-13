# GEMINI.md

## Project Overview

This project is a Python-based web application that provides a dashboard for managing SMS messages stored in a MySQL database by the Gammu SMSD service.

The application is built with the Flask web framework and uses Tailwind CSS for styling. It features a real-time, responsive user interface for viewing, marking as read, and deleting SMS messages. The backend is a single-file Flask application that connects to a MySQL database to perform these actions. The project also includes an optional Telegram bot for notifications.

Dependencies are managed using Poetry.

## Building and Running

### Prerequisites

- Python 3.13+
- Poetry
- MySQL server
- Gammu

### Installation

1.  **Clone the repository:**
    ```bash
    git clone <repository-url>
    cd sms-dashboard
    ```

2.  **Install dependencies using Poetry:**
    ```bash
    poetry install
    ```

3.  **Set up the environment:**
    Create a `.env` file in the project root and populate it with the necessary database credentials and other configuration options. An example can be found in the `README.md` file. A new `TELEGRAM_MASKAN_CHAT_ID` variable can be added to send messages containing the word 'maskan' to a different Telegram group.

### Running the Application

-   **Development:**
    To run the application in development mode, use the following command. This will start the Flask development server and the Telegram bot (if configured).

    ```bash
    poetry run sms-dev
    ```

-   **Production:**
    For production, the application is served using Gunicorn. The following command will start the Gunicorn server and the Telegram bot.

    ```bash
    poetry run sms-prod
    ```

### Testing

The project uses `pytest` for testing. To run the tests, execute the following command:

```bash
poetry run pytest
```

## Development Conventions

-   **Code Style:** The Python code generally follows the PEP 8 style guide.
-   **Dependencies:** All Python dependencies are managed via `pyproject.toml` and `poetry.lock`.
-   **Testing:** Tests are located in the `tests/` directory. The project uses `pytest` for running tests, with assertions made using the standard `assert` keyword.
-   **CI/CD:** A GitHub Actions workflow is configured to automatically build and publish the package to PyPI whenever changes are pushed to the `main` branch.
