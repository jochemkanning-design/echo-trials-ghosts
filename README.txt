ECHO TRIALS — COMMUNITY GHOSTS

STATUS OF THIS PACKAGE
The client and server are implemented. Public hosting is NOT configured in
this ZIP. Until you connect a service, Online best automatically uses your
personal best, or a labelled built-in challenge when you have no best yet.
Do not advertise live community records before completing the steps below.

QUICK PRIVATE TEST ON YOUR WINDOWS PC
1. Extract the whole ZIP to a new folder. Keep your old game folder.
2. Close older game/server console windows.
3. Run START_COMMUNITY_TEST.bat (Python 3.11 or newer).
4. Open http://127.0.0.1:8765/?localghosts=1 in Chrome and Edge.
5. In Chrome: expand Community ghosts & sharing; enter a nickname; enable
   Share my best runs. Finish a course alive. Wait for "published".
6. In Edge: choose the SAME course; click Refresh rivals; then start/retry.
   You should see the Chrome player's nickname and their ghost.
7. Keep the console window open. Ctrl+C stops both local servers.
The local service only accepts connections from this PC. It cannot provide
ghosts to internet visitors. Its test database is outside the game folder:
%LOCALAPPDATA%\EchoTrialsCommunityTest\ghosts.sqlite3

PUBLIC FREE-TIER SETUP
Use itch.io for the game, Render Free for the Python service, and Neon Free
for persistent PostgreSQL. Their free tiers have usage limits. Stay on Free
plans; do not enable paid upgrades or additional paid resources.
Account creation/sign-in must be completed in your own accounts.

A. PERSISTENT DATABASE
1. Open https://neon.com and create a Free project for Echo Trials.
2. Choose a region near the service region you intend to use.
3. Copy the PostgreSQL connection string from Connect. Keep SSL enabled;
   prefer the pooled connection string. Treat the entire string as a secret.
4. Do NOT paste this string into the game, a public repository, or itch.io.

B. HOST THE REPLAY SERVICE
1. Put the CONTENTS of online_service/ in a new Git repository. It should
   contain app.py, Dockerfile, requirements.txt, render.yaml and physics/.
   Do not add a local database, .env file, or account credentials.
2. Open https://render.com and create a Web Service from that repository.
3. Use Docker, the included Dockerfile, and the FREE instance type.
4. Add an environment variable DATABASE_URL containing the Neon connection
   string. Configure it as a server-side secret.
5. Set health check path to /v1/health. The Dockerfile handles the port.
6. ALLOWED_ORIGINS defaults to * because this is a public, cookie-free API.
   An exact allowlist can be configured later. Itch embeds run under an
   itch CDN origin, which is different from your project page's origin.
7. Deploy. Copy the HTTPS service URL shown by Render.
8. Open SERVICE-URL/v1/health. It should identify Echo Trials Ghosts,
   protocol 1, 10 courses, and server_replayed_inputs validation.
The included render.yaml is an alternative Blueprint setup for the same
FREE service. It prompts for DATABASE_URL. Do not provision Render's free
Postgres for long-term records: that product has a limited lifetime.

C. CONNECT AND UPLOAD THE GAME
1. In your extracted game folder, run CONFIGURE_ONLINE.bat.
2. Paste ONLY the public HTTPS Render service URL (not DATABASE_URL).
3. The script checks the service, writes online_config.js, and creates
   Echo_Trials_Online_Ready_For_Itch.zip beside the extracted game folder.
4. Upload that new ZIP to your EXISTING itch.io project. Select HTML and
   mark the ZIP as playable in the browser. Use click-to-launch fullscreen
   and scrollbars; leave Mobile Friendly off.
5. Keep pricing at $0 or donate (optional tips), or No payments.
6. Test the itch preview in two different browser profiles before publishing.
   Confirm that one successful run becomes the other player's named rival.
7. Keep the same itch project and stable hosting address for returning saves.
If configuring without Windows, run:
  python CONFIGURE_ONLINE.py --api https://YOUR-SERVICE.onrender.com

FREE-TIER BEHAVIOUR
Render Free can sleep while idle and take time to wake. The game never waits
for it: personal/challenge ghosts work immediately; online fetches retry in
menus/results. Upload failures stay queued locally while sharing is enabled.
Do not use local SQLite on Render Free: its files disappear on restart.
The service refuses to boot there without DATABASE_URL to prevent that loss.
If service/database quotas are reached, offline play and personal saves work.

PLAYER CHOICES
- Online best: another player's champion for this exact course version.
- My best: your own best finish. Off: hide ghosts but keep recording.
- Online opponent / Next rival above me: nearest better score/time, or the
  champion if no better rival exists. Your own public identity is excluded.
- Sharing starts OFF. A nickname and opt-in enable automatic best submissions.
- Your nickname, score, time and replay become public. No email is requested.
- Share this course's best uploads a compatible saved best manually.
- Remove my published ghosts deletes your public records, not local runs.
- Turning sharing off stops future submissions and clears queued uploads.
- Your sharing identity is saved in this browser separately from run backups.
  Keep this browser's site data if you want to update/remove the same public
  records. A save-file restore transfers personal runs, not public ownership.
- V still watches ALL personal attempts together from the menu/results.

RANKING AND COMPATIBILITY
Successful runs only; highest score wins, fastest time breaks ties. Equal
score/time keeps the existing record. Each player has one public best per
course. The original ten layouts and physics files are unchanged, preserving
their record keys and existing local archives.
Old personal recordings remain viewable. Record a new finish in this version
to share verified inputs; an old replay without inputs cannot be verified.

WHAT THE SERVER CHECKS
The service does not trust submitted score, finish time, positions or flowers.
It replays bounded, validated input samples using the unchanged game physics,
then requires a living finish with both flowers and matching score/time.
Impossible runs, wrong course versions and forged results are rejected.
This is not proof that a human played: valid automated input sequences can
still be submitted. No invasive anti-cheat or player surveillance is included.

OPERATIONS
Keep DATABASE_URL server-side. Back up PostgreSQL using your database host's
export tools or pg_dump. Keep one server worker as configured; the process
limits concurrent verification work and limits requests per identity/source.
The default archive cap is 5,000 player/course records. It refuses new records
when full while continuing to accept improvements to existing records.
Do not change the physics or levels on only one side: update both client and
service together. Changed level versions intentionally get separate records.

OFFICIAL REFERENCES (checked October 2026)
https://itch.io/docs/creators/html5
https://render.com/docs/free
https://render.com/docs/docker
https://neon.com/docs/introduction/plans

INSTRUCTIONS TO GIVE ANOTHER LLM WITH YOUR HOSTING ACCOUNT ACCESS
Deploy the included online_service as a Render FREE Docker web service backed
by Neon FREE PostgreSQL. Use the existing physics files unchanged. Store the
connection string only as Render's secret DATABASE_URL. Do not enable paid
plans or put secrets into the browser ZIP. Verify /v1/health, then run the
included CONFIGURE_ONLINE.py against the public HTTPS API URL to generate the
itch upload ZIP. Upload to my existing itch project and test two independent
browser profiles: publish a successful run in one, fetch/play its ghost in
the other. Also test Off, My best, server outage fallback and save reload.
Report the actual service URL and observed tests; do not claim deployment
success until the service and shared ghost flow work.
