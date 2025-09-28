# Telegram Photo Gallery

A web-based photo gallery that uses your personal Telegram account as a powerful, private, and unlimited backend for storing and managing photo albums.

This project consists of a Python backend powered by FastAPI and Telethon, and a dependency-free Vanilla JavaScript frontend.


## Features

-   **Secure Telegram Login**: Authenticate securely using your phone number with OTP and 2FA password support, directly through the official Telegram API.
-   **Album Management**:
    -   Automatically lists your Telegram groups (that were created by the app).
    -   Create new groups (albums) directly from the web interface.
    -   Delete entire groups.
-   **Photo Gallery**:
    -   Displays photos from a selected group in a responsive grid layout.
    -   Lazy loading for thumbnails to improve performance.
    -   Click on any photo to view it in a full-size lightbox.
-   **Photo Management**:
    -   Download full-resolution images.
    -   Upload multiple photos at once with a modern drag-and-drop UI.
    -   Select and delete multiple photos from an album.
-   **Modern UI**: A clean, single-page application interface with a sidebar for albums and a main content area for photos.

## Technology Stack

-   **Backend**: Python 3, FastAPI, Telethon
-   **Frontend**: HTML5, CSS3, Vanilla JavaScript (no frameworks)
-   **Server**: Uvicorn ASGI server

## Setup and Installation

Follow these steps to get the project running on your local machine.

### 1. Prerequisites

-   Python 3.8+
-   A Telegram account

### 2. Get Telegram API Credentials

You need to obtain an `API_ID` and `API_HASH` from Telegram. This is a one-time setup.

1.  Go to [my.telegram.org](https://my.telegram.org) and log in with your phone number.
2.  Click on **API development tools**.
3.  Fill in the "App title" and "Short name" (e.g., "Photo Gallery App").
4.  You will be given your `api_id` and `api_hash`. **Keep these secret!**

### 3. Clone the Repository

```bash
git clone <your-repository-url>
cd <repository-directory>
```

### 4. Backend Setup

1.  **Create and activate a virtual environment:**
    ```bash
    # For macOS/Linux
    python3 -m venv venv
    source venv/bin/activate

    # For Windows
    python -m venv venv
    .\venv\Scripts\activate
    ```

2.  **Install the required Python packages:**
    ```bash
    pip install -r requirements.txt
    ```

3.  **Set Environment Variables:**
    The application requires your Telegram API credentials. Set them as environment variables.

    -   **On macOS/Linux:**
        ```bash
        export TG_API_ID="YOUR_API_ID"
        export TG_API_HASH="YOUR_API_HASH"
        ```

    -   **On Windows (Command Prompt):**
        ```bash
        set TG_API_ID="YOUR_API_ID"
        set TG_API_HASH="YOUR_API_HASH"
        ```

### 5. Running the Application

1.  **Start the Backend Server:**
    Run the following command in your terminal from the project's root directory:
    ```bash
    uvicorn main:app --reload
    ```
    The server will start, typically at `http://127.0.0.1:8000`.

2.  **Launch the Frontend:**
    Simply open the `index.html` file in your web browser. The frontend is designed to communicate with the local server you just started.

## How It Works

-   The **FastAPI backend** serves as a bridge between the frontend and the Telegram API.
-   **Telethon** is used to handle all interactions with Telegram, including authentication, fetching messages (photos), and managing groups.
-   The **Vanilla JS frontend** makes API calls to the local FastAPI server to perform all actions. Photos are streamed directly from Telegram through the backend to ensure privacy and security.
-   Groups created by this application have a specific "about" text, which the backend uses to filter and display only the relevant groups as "albums".

## API Endpoints

The backend provides the following key API endpoints:

-   `GET /api/auth/status`: Check if the user is logged in.
-   `POST /api/login/send-code`: Start the login process.
-   `POST /api/login/verify`: Complete the login with OTP/password.
-   `POST /api/logout`: Log the user out.
-   `GET /api/my-groups`: Get a list of the user's albums (groups).
-   `POST /api/groups`: Create a new group.
-   `DELETE /api/groups/{group_id}`: Delete a group.
-   `GET /api/photos`: Get photos from a specific group.
-   `POST /api/upload`: Upload a new photo to a group.
-   `DELETE /api/photos/{message_id}`: Delete a specific photo.
-   `GET /api/photos/{message_id}/full`: Get the full-resolution image file.

---
Developed by ANSH RAJ.