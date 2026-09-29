# GitHub secret-scan fix

1. Use this project as a fresh working folder.
2. `.env` contains local secrets and is ignored by `.gitignore`; do not upload it to GitHub.
3. Commit `.env.example` instead.
4. If you previously committed the old `app.py` containing credentials, do NOT push that old Git history. Create a fresh Git history for this cleaned copy, or remove the secrets from the existing history before pushing.
5. Rotate any Google/Gemini credentials that were previously exposed in a repository or public location.

For a fresh repository from this cleaned ZIP:

```powershell
# Run inside the extracted project folder
git init
git add .
git status
# Confirm .env is NOT listed
git commit -m "Initial secure QueueFree project"
git branch -M main
git remote add origin YOUR_REPOSITORY_URL
git push -u origin main
```

For local HTTP testing keep `COOKIE_SECURE=0`. Change it to `1` only when the app is served over HTTPS.
