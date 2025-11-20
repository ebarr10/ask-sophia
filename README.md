# ask-sophia

**Sophia** is a Slack bot that analyzes past messages to automatically generate helpful responses, allowing users to quickly resolve common or repeated questions without waiting for someone else to reply.

This project was built as part of a hackathon at **Foreman**.

---

## 🚀 Features

- Automatically suggests answers based on past Slack conversations  
- Helps reduce repeated questions  
- Integrates seamlessly with Slack using the [Bolt.js](https://slack.dev/bolt-js) framework
- Can be extended with custom logic or AI integrations (e.g. OpenAI API, Gemini, etc)  

---

## 🛠️ Setup

### 1. Create a Slack App

1. Go to [https://app.slack.com/](https://app.slack.com/) and sign in to your Slack workspace
2. Navigate to [https://api.slack.com/apps](https://api.slack.com/apps)
3. Click **"Create New App"** → **"From manifest"**
4. Select your workspace/organization to link the app to
5. Copy and paste the contents of `manifest.json` into the manifest editor
6. Click **"Create"** to create the app

### 2. Get Your Slack Tokens

1. In your app's settings, go to **"OAuth & Permissions"** under **"Features"**
2. Scroll down to **"Bot User OAuth Token"** and copy the token (starts with `xoxb-`)
3. Go to **"Basic Information"** → scroll to **"App-Level Tokens"**
4. Click **"Generate Token and Scopes"**
5. Name it (e.g., "socket-mode") and add the `connections:write` scope
6. Copy the generated token (starts with `xapp-`)

### 3. Clone the repository

```bash
git clone https://github.com/<your-org-or-username>/ask-sophia.git
cd ask-sophia
```

### 4. Install dependencies

```bash
pip install -r requirements.txt
```

### 5. Set up environment variables

Create a `.env` file in the root directory with the following variables:

```env
SLACK_BOT_TOKEN=xoxb-your-bot-token-here
SLACK_APP_TOKEN=xapp-your-app-token-here
GEMINI_API_KEY=your-gemini-api-key-here
GEMINI_MODEL=your-gemini-model-name
```

Replace the placeholder values with:

- `SLACK_BOT_TOKEN`: The Bot User OAuth Token from step 2
- `SLACK_APP_TOKEN`: The App-Level Token from step 2
- `GEMINI_API_KEY`: Your Google Gemini API key (get it from [Google AI Studio](https://makersuite.google.com/app/apikey))
- `GEMINI_MODEL`: The Gemini model name (e.g., `gemini-pro`, `gemini-1.5-pro`)

### 6. Run the bot

```bash
python sophia.py
```
