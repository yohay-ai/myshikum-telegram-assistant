# MyShikum Telegram Assistant

בוט Telegram פרטי שמתחבר לאזור האישי של אגף השיקום, בודק עדכונים בפניות ומאפשר להכין פנייה חדשה ולשלוח אותה רק אחרי מסך אישור מפורש.

## מה הבוט עושה

- `/help` - מציג את רשימת הפקודות הזמינות.
- `/autocheck_start`, `/autocheck_stop`, `/autocheck_status` - שליטה במצב הבדיקה האוטומטית לזמן הריצה הנוכחי.
- `/check` - קורא את רשימת הפניות ומדווח רק על שינויים.
- `/new_request` - פותח את טופס הפנייה הרשמי, קורא ממנו בזמן אמת קטגוריות ותתי-קטגוריות, ואוסף טקסט וקבצים.
- `/review` - מציג לפני שליחה את הערוץ, הקטגוריה, תת-הקטגוריה, הטקסט המלא ושמות הקבצים.
- `/cancel` - מבטל את הפעולה ומוחק קבצים זמניים.
- `/logout` - מוחק את מצב ההתחברות המקומי.
- `/diagnostics` - מציג גרסה, זמן ריצה, מצב Chromium/session/autocheck ופעולה פעילה, בלי מזהים או תוכן אישי.
- `/whoami` - מציג את מזהה הצ'אט לצורך הגדרה ראשונית.

הבוט לא שולח פנייה על סמך טקסט בלבד. שליחה דורשת לחיצה על "אישור ושליחה" במסך הסיכום. האישור חד-פעמי ופג אחרי 10 דקות. אם לא ניתן לאמת הצלחה, הבוט לא מנסה שוב אוטומטית כדי לא ליצור פנייה כפולה.

## התקנה

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
playwright install chromium
cp .env.example .env
```

ערוך את `.env` המקומי והפעל:

```bash
python rehab_checker_bot.py
```

## בדיקה אוטומטית תקופתית

המצב תמיד כבוי באתחול. מגדירים את המרווח ואז מפעילים במפורש עם `/autocheck_start`:

```dotenv
AUTOCHECK_INTERVAL_MINUTES=60
```

המרווח המינימלי הוא 15 דקות כדי לא להעמיס על MyShikum או לעורר הגנות חשבון. `60` הוא ערך הדוגמה וברירת המחדל בלבד, לא בחירה קשיחה עבור המשתמש. לאחר כל מחזור מתווסף jitter של ±10%. כשלונות גורמים ל-backoff מעריכי עד שש שעות.

הבודק משתמש ב-session השמור הקיים ובאותו מנגנון השוואת שינויים של `/check`. הוא לא מפעיל מחזור מקביל לפעולה ידנית, אינו שומר לוח זמנים או פרטי כשל, ומוסיף לדיסק רק את מצב ההשוואה המינימלי שכבר קיים. Telegram מקבל הודעה רק על שינוי משמעותי או בפעם הראשונה שסוג כשל בר-פעולה מופיע, למשל צורך בהתחברות מחדש. מחזור תקין ללא שינוי נשאר שקט.

הפקודות `/autocheck_start` ו-`/autocheck_stop` מפעילות ועוצרות את הלולאה בתהליך הנוכחי בלבד, בלי לשנות את `.env`. אחרי כל אתחול צריך להפעיל מחדש עם `/autocheck_start`. `/autocheck_status` מציג שהפעלה באתחול כבויה תמיד, את המצב הנוכחי ואת המרווח. עצירת הבוט ממתינה לסיום נקי של לולאת הבדיקה וסוגרת דפדפנים פעילים.

## Docker

בנה את ה-image מקומית:

```bash
docker build -t myshikum-telegram-assistant .
mkdir -p .rehab_checker_state
docker run --rm --init \
  --env-file .env \
  -v "$PWD/.rehab_checker_state:/app/state" \
  myshikum-telegram-assistant
```

ל-image ול-Compose יש healthcheck שמוודא שה-event loop חי, שההגדרה תקינה ושקובץ ה-heartbeat מתעדכן. Compose מסמן את השירות כ-`unhealthy` אחרי שלושה כשלים רצופים.

ה-container רץ כמשתמש לא-מורשה (UID 10001). יש לתת לו הרשאת כתיבה לתיקיית המצב הממופה, למשל `sudo chown -R 10001:10001 .rehab_checker_state`. אין להעתיק `.env` או את תיקיית המצב ל-image; שתיהן מוחרגות דרך `.dockerignore`.

משתני הסביבה הנתמכים: `TELEGRAM_TOKEN`, `AUTHORIZED_CHAT_ID`, `PERSONAL_ID`, `OTP_CHANNEL`, `OTP_CONTACT`, `HEADLESS`, `BROWSER_EXECUTABLE`, `STATE_DIR`, `LOG_LEVEL` ו-`AUTOCHECK_INTERVAL_MINUTES`. בתוך Docker ברירת המחדל של `STATE_DIR` היא `/app/state`.

GitHub Actions בונה את ה-image ומריץ בו בדיקות בכל pull request ובכל push ל-`main`. שלב הבנייה והבדיקות משתמש רק ב-`contents: read`. אחרי push מוצלח ל-`main`, job נפרד עם `packages: write` מפרסם ל-`ghcr.io/yohay-ai/myshikum-telegram-assistant` עם התגיות `latest` ו-`sha-<commit>`; pull requests לעולם אינם מפרסמים image.

### Docker Compose מ-GHCR

הקובץ `compose.ghcr.yml` משתמש ב-`ghcr.io/yohay-ai/myshikum-telegram-assistant:latest`, טוען secrets ומשתני הגדרה מתוך `.env` בלבד, ושומר את מצב ההתחברות ב-volume בשם `myshikum-state`.

```bash
cp .env.example .env
# ערוך את .env מקומית; אל תעלה אותו ל-Git
docker compose -f compose.ghcr.yml pull
docker compose -f compose.ghcr.yml up -d
docker compose -f compose.ghcr.yml logs -f
```

לעצירה בלי למחוק את מצב ההתחברות:

```bash
docker compose -f compose.ghcr.yml down
```

התגית `latest` עוקבת אחרי `main`. לפריסה מקובעת אפשר להחליף אותה בתגית `sha-<commit מלא>` שה-workflow מפרסם. אם החבילה אינה ציבורית, התחבר פעם אחת ל-GHCR עם token בעל `read:packages`; אין לשמור את ה-token בקובץ Compose או ב-`.env`.

## לוגים

`LOG_LEVEL` שולט ברמת הלוג (ברירת מחדל `INFO`). הלוג מכיל רק שמות אירועים תפעוליים וסוגי שגיאות. אין בו token, קוד חד-פעמי, תעודת זהות, פרטי קשר, chat ID, תוכן פנייה, שמות או תוכן צרופות, cookies או מצב session. גם לוגים מפורטים של ספריות HTTP, Telegram ו-Playwright אינם מופעלים.

## אבטחה ופרטיות

- אין לשמור token, תעודת זהות, מספר טלפון, כתובת מייל או chat ID בקוד או ב-Git.
- `.env` ומצב הדפדפן מוחרגים מ-Git. קובצי המצב נשמרים בהרשאות משתמש בלבד.
- קוד חד-פעמי תקף לחמש דקות, אינו נרשם ללוג, והבוט מנסה למחוק את הודעת Telegram שמכילה אותו.
- צרופות נשמרות בתיקייה זמנית בהרשאות מצומצמות ונמחקות בסיום או בביטול.
- האתר הרשמי עשוי להשתנות. הבוט משתמש בטקסטים ובתפקידי נגישות ולא ב-IDs פנימיים או ב-API פרטי לא מתועד.
- לפני שימוש אמיתי בשליחת פנייה, מומלץ להריץ `HEADLESS=false` ולבדוק את הזרימה מול החשבון שלך בלי ללחוץ על אישור השליחה.

## בדיקות

```bash
python -m unittest discover -s tests -v
python -m py_compile rehab_checker_bot.py
```

הבדיקות אינן מתחברות לחשבון ואינן שולחות פנייה. הן מדמות את מסך האתר ואת שלב השליחה.
