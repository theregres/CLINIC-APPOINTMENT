How it works

Why Flask and Supabase? Flask is a small Python web framework, and Supabase is a hosted Postgres database. That keeps the data safe and online, not in a local file.
How is it structured? app.py holds the routes (the pages), database.py holds DataManager (all the SQL), models.py holds the classes, and the templates/ folder holds the HTML pages.
What are the three roles? Admin, doctor and patient. Each can only open their own pages.

Security (teachers like this one)

How are passwords stored? I believe they’re hashed, not plain text. I haven’t re-checked that in security.py, so look before you say it.
What stops brute-force logins? LoginLimiter counts failed attempts per account and per IP address, then locks the account for a while.
Why do your forms have a hidden csrf_token? It blocks forged requests from other sites.
How do you protect the database? Row-level security is on for all tables. The Supabase service key lives only in Render’s environment settings, not in GitHub.

Features

How do you stop double-booking? A doctor’s slot and a patient’s time can’t be booked twice, and blocked dates are respected.
What are the unique IDs for? Each account gets a number in 000-000 format, shown beside names so two people with the same name can’t be confused.
What happens if the database is lost or hacked? The admin’s Backups page makes a full copy, and a weekly automatic backup runs. Restoring needs the admin password and typing RESTORE, and it saves a safety copy first.
Why 10 per page? The lists grow without limit, so paging keeps pages fast and readable.

Likely trick questions

“Did you test it?” Yes, with the automated checks from your testing slide. Be honest that backups were tested locally, not against real Supabase Storage.
“Where did the images come from?” Have the source ready. I couldn’t find it in my records, so check your download history.
“What would you improve?” Good answers: email verification on a real mail server, more than one subclass of Record, and a paid Render plan so the site doesn’t sleep.
“What’s the free-plan limitation?” The Render site sleeps when idle, so the first load is slow. Open it a few minutes before presenting.

If you want, I can write these as a one-page cheat sheet, or quiz you on them one at a time. Before the presentation, it’s worth double-checking the password-hashing answer in security.py. I can do that for you now if you’d like.
