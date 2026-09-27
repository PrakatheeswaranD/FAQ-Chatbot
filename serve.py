import os

from waitress import serve

from app import app


if __name__ == "__main__":
    serve(
        app,
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        threads=int(os.getenv("WAITRESS_THREADS", "8")),
        channel_timeout=int(os.getenv("WAITRESS_CHANNEL_TIMEOUT", "45")),
        cleanup_interval=30,
        ident="kit-faq-chatbot",
    )
