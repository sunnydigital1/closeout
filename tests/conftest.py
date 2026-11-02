"""Make tests independent of whatever is in a developer's local .env file."""
import os

# python-dotenv never overrides variables that already exist, so these win.
os.environ["AI_PROVIDER"] = "none"
os.environ["GEMINI_API_KEY"] = ""
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["PAYPAL_CLIENT_ID"] = ""
os.environ["PAYPAL_CLIENT_SECRET"] = ""
