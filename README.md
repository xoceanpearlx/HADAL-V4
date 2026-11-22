# HADAL-V4
AI file organizer/chatbot. Version 4

HADAL includes a Windows desktop assistant and a separate hosted website.
The desktop app can organize files in folders you select. The website offers
chat and grammar checking, but cannot access files on your computer.

## Desktop app

### Start HADAL

From PowerShell in the project folder:

```powershell
python -m pip install -r requirements.txt
python .\file_assistant.py
```

The desktop app's grammar checker and file tools run locally. Internet
research in chat requires a valid `TINYFISH_API_KEY` in a `.env` file beside
`file_assistant.py`. Local file commands and grammar checking can be used
without that key. Do not share or commit `.env`.

### Use the file organizer

1. Open **File organizer**, add a folder, and select it.
2. Choose **Organize by** to select a grouping rule:
   - **File type**: one folder per extension.
   - **Name (A–Z)**: group by the first letter; other names go in `_OTHER`.
   - **File size**: use the default bands: under 1 MB, 1–100 MB, and 100 MB
     or more.
   - **Date modified**: group by year and month.
3. Optionally choose a preview sort order, then click **Preview**. Preview
   shows the planned destinations; it does not move files.
4. Click **Apply preview** and confirm to move the files.

The organizer moves only files directly inside the selected folder; it does
not reorganize files in subfolders. Preview sorting changes the preview and
confirmation order, not how Windows displays files inside folders.

**Review duplicates** scans subfolders and reports possible matches based on
filename and size. It does not read file contents or delete anything.

You can also enter these commands in chat. Folder numbers correspond to the
selected-folder list and start at 1:

```text
list files in folder 1
search filenames for report
preview organization for folder 1 by modified
review duplicates in folder 1
apply organization
```

The organization method may be `type`, `name`, `size`, or `modified`.
Applying an organization still requires confirmation.

## Hosted website

The website version provides password-protected chat and grammar checking.
Chat research is sent to TinyFish; grammar checking uses the local HADAL
checker on the server. Keep API keys on the server, never in JavaScript or
HTML. Choose a private site password with at least 14 characters.

### Run the website locally on Windows

1. Create a private `.env` file beside `webapp.py`, using `.env.example` as a
   reference. Set `TINYFISH_API_KEY`, `HADAL_ACCESS_PASSWORD`, and
   `HADAL_SECRET_KEY`. Use a private password of at least 14 characters and a
   random secret key.
2. Install dependencies and start the web app:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   python .\webapp.py
   ```

3. Open `http://127.0.0.1:5000` and sign in with your site password.

### Host on Render

1. Push the project to a **private** GitHub repository. Do not include `.env`;
   it is excluded by `.gitignore`.
2. In Render, choose **New → Blueprint** and connect the repository. Render
   reads `render.yaml` and prompts for `TINYFISH_API_KEY` and
   `HADAL_ACCESS_PASSWORD`.
3. Provide a newly generated TinyFish key and a private password of at least
   14 characters. Render generates `HADAL_SECRET_KEY` for the site.
4. Deploy, then open the HTTPS URL Render gives you and sign in.

Use the hosting provider's secret/environment-variable settings for
credentials. Do not commit keys or passwords. The web service needs outbound
internet access to reach TinyFish. Research uses a streaming connection and
may take time; choose a hosting plan that supports long-lived streaming
responses.

## Tests

Run all tests from the project folder:

```powershell
python -m unittest discover -s tests -v
```

To run only the desktop organizer tests:

```powershell
python .\tests\test_organization_methods.py
```
