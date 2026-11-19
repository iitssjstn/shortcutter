# Shortcutter

Webapp die een YouTube-link of een geüpload videobestand omzet in verticale shorts (1080×1920),
met woord-voor-woord ondertitels, een dashboard om clips te bekijken, bij te stellen en te
downloaden, een accountsysteem op uitnodiging, sleutelbeheer in de interface en een
(eerlijke) schatting van het aantal views per clip.

Alles draait in één Docker-container. GitHub Actions bouwt de image en zet hem op
`ghcr.io/iitssjstn/shortcutter`.

> **Let op: auteursrecht.** Gebruik alleen video's van jezelf of waarvoor je toestemming hebt.
> Knippen uit andermans video's kan auteursrecht schenden, en YouTube kan kanalen met
> "reused content" demonetiseren of beperken. Shortcutter uploadt niets automatisch: jij
> bekijkt en post zelf.

---

## Installatie op de server

Je hebt alleen Docker (met de compose-plugin) en het bestand `docker-compose.yml` uit deze repo nodig.

```bash
mkdir shortcutter
```

```bash
cd shortcutter
```

Zet `docker-compose.yml` in deze map, en start:

```bash
docker compose pull && docker compose up -d
```

Is de package op GitHub privé, log dan eerst in bij de registry met een personal access
token (classic) met het recht `read:packages`:

```bash
docker login ghcr.io -u <je-github-gebruikersnaam>
```

Updaten naar een nieuwe versie gaat met hetzelfde commando (`docker compose pull && docker compose up -d`).

### Instellingen in docker-compose.yml

Alleen niet-geheime instellingen staan in de compose:

| Variabele | Standaard | Uitleg |
|---|---|---|
| `WHISPER_MODEL` | `small` | `tiny`, `base`, `small`, `medium`, `large-v3`… Ook te wijzigen in Instellingen (admin). |
| `WHISPER_DEVICE` | `cpu` | `cuda` voor een NVIDIA-GPU (vereist een GPU-runtime). |
| `WHISPER_COMPUTE` | `int8` | `int8` op cpu, `float16` op cuda. |
| `WHISPER_THREADS` | `0` | Aantal CPU-threads voor Whisper (0 = automatisch). |
| `CT2_FORCE_CPU_ISA` | automatisch | `GENERIC` (geen AVX) of `AVX` voor oude CPU's. Wordt normaal zelf gedetecteerd. |
| `WHISPER_VAD` | `true` | `false` schakelt de spraakdetectie (onnxruntime) uit. |
| `COOKIE_SECURE` | `false` | Zet op `true` zodra de app achter HTTPS draait (reverse proxy). |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | IP van je reverse proxy, zodat rate limiting het echte client-IP ziet. |

Er zijn **geen** sleutels, wachtwoorden of secrets nodig om de app te starten.

Data staat in `./data` (database, bron- en clipbestanden, master key). Het Whisper-model
wordt bij het eerste project gedownload naar het volume `whisper-cache`.

## Eerste setup

1. Haal de eenmalige setupcode uit de logs:

   ```bash
   docker compose logs shortcutter
   ```

2. Open `http://<server>:8000/setup`, vul de setupcode in en maak het eerste account aan
   (wachtwoord minimaal 12 tekens). Dit account wordt beheerder.
3. Zodra er een account bestaat, geeft `/setup` een 404 en is de code ongeldig. Bij een
   herstart zonder account wordt een nieuwe code geprint.

Nieuwe gebruikers voeg je toe via **Gebruikers → Uitnodigen**: je krijgt een link (48 uur
geldig, werkt één keer) die je zelf doorstuurt. Er is geen open registratie.

## API-sleutels toevoegen

Ga als beheerder naar **API-sleutels**:

- **LLM** (aanbevolen): naam, base-URL (standaard Groq: `https://api.groq.com/openai/v1`),
  model (bijv. `llama-3.3-70b-versatile`) en API-key. Elke OpenAI-compatibele API werkt. Je
  kunt meerdere configuraties opslaan en er één actief maken. Met **Test sleutel** zie je of
  hij werkt (geldig, ongeldig of rate limit).
- **YouTube Data API** (optioneel): om het kanaalgemiddelde op te halen bij Instellingen.
- **cookies.txt** (optioneel): als YouTube downloads blokkeert ("Sign in to confirm you're
  not a bot"). Exporteer cookies in Netscape-formaat met een browserextensie.

Sleutels worden versleuteld (Fernet) in de database opgeslagen en na opslaan nooit meer naar
de browser gestuurd: je ziet alleen een masker zoals `gsk_…a1b2`. Wijzigingen staan in de
auditlog (zonder waarde). Zet sleutels **nooit** in `.env`, `docker-compose.yml` of de repo.

Zonder LLM-sleutel werkt alles ook: fragmenten worden dan gekozen met een heuristiek
(spreekdichtheid), en het project vermeldt dat.

## Back-up

Maak een back-up van de hele map `data/`. Die bevat:

- `db.sqlite` (accounts, projecten, versleutelde sleutels),
- `output/` (gerenderde clips) en `input/` (bronbestanden, worden na de retentieperiode opgeruimd),
- **`.master_key`**: de sleutel waarmee de API-sleutels versleuteld zijn.

Een back-up van `data/` bevat dus zowel de versleutelde sleutels als de master key. Wil je
die scheiden, geef de master key dan mee als Docker secret: maak een bestand met een lange
willekeurige tekst en zet in `docker-compose.yml` het `secrets`-blok aan (zie de commentaren).
De app leest dan `/run/secrets/app_master_key` en maakt geen `data/.master_key` aan.
Bewaar het secret-bestand apart van de back-up. Let op: wissel je van master key, dan kunnen
bestaande sleutels niet meer ontsleuteld worden en moet je ze opnieuw invullen.

## Gebruik

1. **Dashboard**: plak een YouTube-link of upload een bestand, kies aantal clips en min/max duur.
   Je ziet de status, voortgang en je plek in de wachtrij. De worker verwerkt één job tegelijk.
2. **Project**: per clip een speler, titel, hook, viral score en geschatte views. Je kunt trimmen,
   ondertitels aan/uit zetten, hertitelen, opnieuw renderen, verwijderen en een clip als
   "gepost" markeren met platform en link, en later de echte views invullen.
   Met **Clip handmatig toevoegen** kies je zelf start en einde (bronspeler en transcript ernaast).
3. **Downloaden**: per clip (met of zonder ondertitels, of los de `.srt`/`.ass`), per selectie
   of het hele project als zip (met `clips.json` en een tekstbestand met titels en beschrijvingen).
   Downloads werken ook als de LLM-API of internet niet bereikbaar is.
4. **Statistieken**: schatting versus echte views en welke soorten clips het best presteren.

### Bij storingen

- **Download geblokkeerd**: upload het bronbestand zelf op de projectpagina; het project gaat
  verder waar het was. Of voeg een cookies.txt toe.
- **LLM-API faalt**: het project wordt gewoon klaar met de heuristiek en een waarschuwing.
- **Whisper-model laden mislukt**: kies een kleiner model bij Instellingen en klik op *Opnieuw proberen*.
  De eerste keer moet de container het model kunnen downloaden van Hugging Face.
- **Container crashte tijdens transcriberen / oude CPU**: Whisper draait in een apart proces, dus een
  crash neemt de app niet meer mee. Op CPU's zonder AVX2 kiest de app zelf een passende instructieset;
  crasht Whisper toch op een CPU-instructie, dan probeert hij het één keer opnieuw in een veilige modus
  (zonder AVX, float32, zonder spraakdetectie). Dat is trager maar werkt op elke x86-CPU. Raakt het
  geheugen vol, dan krijg je daarvan een melding: kies dan een kleiner model (`base` of `tiny`).
  Een taak die de server twee keer laat herstarten, wordt als mislukt gemarkeerd in plaats van eindeloos opnieuw te starten.
- **Model per taak** (Instellingen, admin): een apart LLM-model voor de clipkeuze (sterk model, lange transcripten)
  en voor titels/beschrijvingen (snel en goedkoop). Met komma's geef je terugvalmodellen op: faalt het eerste,
  dan probeert de app het volgende. Op de projectpagina zie je welk model echt antwoordde.
- **Posttekst**: de LLM schrijft per clip een titel, beschrijving en hashtags om te kopiëren (standaard in het
  Engels voor meer bereik; te wijzigen bij Instellingen). Voor bestaande clips: "Posttekst voor alle clips".
- **Beeld niet gevuld (16:9 klein in 9:16)**: kies bij Beeldvulling "Vullen" (per project, als standaard bij
  Instellingen, of per clip met een schuif voor de positie). "Automatisch" vult alleen als er een gezicht in beeld is.
  Gezichten worden gevonden met YuNet (een klein neuraal netwerk in OpenCV, met Haar als terugval). De uitsnede
  volgt de hoofdpersoon soepel; kleine bewegingen worden genegeerd zodat het beeld rustig blijft. Met de schuif
  kies je een vaste positie in plaats van volgen.
- **Gaming / facecam**: kies "Gesplitst". Boven komt de facecam, onder de gameplay; met de schuif verschuif je de
  gameplay naar links of rechts. Het kader van de facecam wordt automatisch herkend: gameplay beweegt, een webcam
  staat (bijna) stil en heeft vaste randen. Klopt het niet, sleep dan op de projectpagina bij "Facecam aanwijzen" een
  rechthoek om de facecam; dat geldt voor alle clips. Is er geen facecam-overlay (bijv. een schermvullende
  webcam-shot), dan wordt het beeld gevuld en volgt het het gezicht.
- **Clip zelf bewerken**: via "✎ Indeling & ondertitels" op een clip open je de editor. Teken op een stilstaand
  beeld het facecam- en gameplay-vak (of één uitsnede bij "Vullen"); de facecam-hoogte past zich aan je facecam aan
  en een live voorbeeld toont de short. Daaronder verbeter je de ondertitelregels en kies je positie (ook "op de
  naad" tussen facecam en gameplay), grootte, kleur en hoofdletters. Opslaan rendert de clip opnieuw; de
  `.srt`/`.ass`-downloads krijgen de verbeterde tekst. Na trimmen gelden weer de ondertitels uit het transcript.
- **Render mislukt**: de ruwe ge-cropte versie en de `.ass` blijven downloadbaar; klik op *Opnieuw renderen*.
- **yt-dlp verouderd**: elke image-build haalt de nieuwste yt-dlp. Start de workflow handmatig
  (Actions → Docker image → Run workflow) en doe daarna `docker compose pull && docker compose up -d`.

### Opruimen

Bij Instellingen (admin) stel je in na hoeveel dagen bronbestanden van afgeronde projecten
verwijderd worden (standaard 7; clips blijven bewaard) en optioneel hele projecten. Je ziet daar
ook het schijfgebruik per project.

## Twitch-clips

Shortcutter accepteert naast YouTube ook Twitch-links (clip, VOD of tijdstempel). Een korte bron, zoals een
Twitch-clip, wordt in zijn geheel één short.

Op de pagina **Twitch** voeg je kanalen toe waarvan je clips mag gebruiken: je **eigen kanaal** of een streamer die
je **toestemming** heeft gegeven (met een vinkje en een notitie over de afspraak). Per kanaal zie je de best bekeken
clips van de afgelopen dag, week, maand of altijd, en maak je er met één klik of in bulk shorts van (standaard
"Gesplitst": facecam boven, gameplay onder). De posttekst krijgt automatisch een credit (`twitch.tv/kanaal`).
Kanalen zijn persoonlijk: elke gebruiker ziet alleen zijn eigen lijst.

Met **Zoeken op game** zoek je een game of categorie (bijv. "GTA") en zie je de meest bekeken clips van alle
streamers in die categorie, per dag, week, maand of altijd. Omzetten kan alleen bij streamers in je kanalenlijst;
bij andere streamers staat een knop om het kanaal toe te voegen zodra je toestemming hebt.

Hiervoor heeft de beheerder een gratis Twitch-app nodig: maak er een op `dev.twitch.tv/console/apps`
(redirect-URL `http://localhost`, categorie *Application Integration*) en vul client-ID en client-secret in bij
**API-sleutels**. Shortcutter leest alleen openbare clipgegevens en plaatst niets op Twitch.

> Clips zijn van de streamer en de game-beelden van de uitgever. Gebruik alleen clips met toestemming, vermeld de
> streamer, en voeg zelf iets toe (ondertitels, selectie, titel): zonder toestemming riskeer je claims of strikes, en
> YouTube kan herplaatste content demonetiseren.

## Compilaties (lange video's)

Onder **Compilaties** maak je van de best bekeken Twitch-clips één liggende video
(16:9, 1920×1080) voor YouTube, bijvoorbeeld een wekelijkse "Best clips of the week".

1. Kies een of meer kanalen uit je lijst (eigen kanaal of met toestemming), de
   periode, het aantal clips en de volgorde (aflopend #10 → #1, beste eerst of zonder nummers).
2. Shortcutter zet de meest bekeken clips klaar. Je kunt clips aan- en uitvinken, de
   volgorde aanpassen, de titel in beeld wijzigen, per clip begin en eind instellen en
   losse cliplinks toevoegen.
3. **Renderen**: per clip een balk linksonder met nummer, titel en kanaal, korte fades
   en gelijk geluidsniveau. Optioneel een introscherm (3 s) en een eindscherm met
   credits (4 s). Het beeld wordt nooit bijgesneden: andere verhoudingen krijgen zwarte randen.
4. Titel en beschrijving worden automatisch geschreven, met hoofdstukken (tijden) en
   credits per streamer. YouTube toont hoofdstukken alleen bij minstens 3 stuks van
   minstens 10 seconden.
5. Download de mp4 of upload hem via je gekoppelde YouTube-kanaal (direct of ingepland).

**Short top-lijst (9:16)**: kies bij Formaat "Short top-lijst". De clips vullen het staande
beeld (smalle vervaagde rand), bovenaan staat een grote titel en links een genummerd lijstje
(1 goud, 2 zilver, 3 brons) dat zich vult terwijl de clips voorbijkomen. "Top 5" in de titel
wordt groen; zet een woord tussen sterretjes voor rood (`Ranking Top 5 *Speed* Moments`).
Maximaal 10 clips en 3 minuten; zonder intro en eindscherm, #shorts gaat automatisch mee.

Klik op een miniatuur (in de compilatie of op de Twitch-pagina) om de clip af te spelen
zonder de pagina te verlaten.

**Wekelijks automatisch**: kies kanalen, dag en tijd (tijdzone uit `TZ`, standaard
Europe/Amsterdam). Op dat moment maakt Shortcutter een concept met de best bekeken clips
van de afgelopen week en rendert het meteen als je dat aanzet. Uploaden gebeurt nooit
vanzelf: je kijkt het eerst na. Met "Nu een concept maken" probeer je het direct uit.

Let op: compilaties van andermans clips ziet YouTube snel als "hergebruikte content",
wat het te gelde maken van je kanaal kan bemoeilijken. Eigen commentaar of eigen
inbreng helpt.

## Nakijken, autopilot en Discord

- **Nakijken** (menu): alle klare clips die nog niet geüpload zijn, met video, titel, beschrijving,
  hashtags en tags. Per clip: Inplannen (op het aanbevolen moment of een eigen tijd), Nu uploaden,
  Opslaan of Overslaan. Of selecteer meerdere en plan ze in één keer in.
- **Aanbevolen uploadmomenten**: vanaf 10 geposte clips met views op basis van je eigen resultaten
  (welke uren het best liepen), anders standaardtijden voor je doelgroep (Engels: Amerikaanse
  middag/avond). Met minimaal x uur ertussen en maximaal y per dag (instelbaar), rekening houdend met
  wat al ingepland staat.
- **Autopilot** (op Nakijken): haalt elke 3–48 uur de nieuwste top-clips op van gekozen kanalen
  (eigen of met toestemming), met een minimum aantal views en een maximum per keer, en maakt er
  shorts van. Al omgezette clips worden overgeslagen. Uploaden gebeurt niet vanzelf.
- **Discord-meldingen** (Mijn account): plak een webhook-URL (versleuteld opgeslagen). Je krijgt een
  bundelbericht als er clips klaarstaan, en meldingen bij compilaties, uploads en de autopilot. Zet
  `PUBLIC_URL` in je compose voor klikbare links.

## Tags en het sterkste moment

- **Tags van wat nu werkt**: bij het schrijven van titels en hashtags zoekt Shortcutter de best
  bekeken video's van de afgelopen 30 dagen over dezelfde game/streamer (YouTube Data API) en geeft
  hun tags, hashtags en titels mee aan de AI. Die kiest daaruit wat echt bij de clip past en vult de
  verborgen tags in; die gaan mee bij het uploaden. Kost 100 quota-eenheden per onderwerp per dag
  (24 uur bewaard). Uit te zetten bij Instellingen (beheer). Tags hebben volgens YouTube een kleine
  invloed; titel, thumbnail en kijkgedrag tellen veel zwaarder.
- **Teaser vooraan**: nieuwe clips beginnen met ±2 seconden van het sterkste moment (de piek die de
  AI aanwijst, anders het luidste moment in het geluid), daarna de clip vanaf het begin. Ondertitels
  schuiven mee. Aan/uit als standaard bij Instellingen en per clip.

## Uploaden naar YouTube

Gebruikers kunnen hun eigen YouTube-kanaal koppelen (**Mijn account**) en clips direct uploaden of een selectie
inplannen (bijv. elke dag om 18:00). Na een geslaagde upload wordt de clip automatisch als gepost gemarkeerd.

Eenmalig door de beheerder (**API-sleutels → YouTube-upload**):

1. In `console.cloud.google.com`: schakel **YouTube Data API v3** in.
2. **OAuth-toestemmingsscherm**: type *Extern*, scopes `youtube.upload` en `youtube.readonly`, publicatiestatus
   **In productie** (in testmodus verlopen koppelingen na 7 dagen; in productie zie je bij het koppelen een
   waarschuwing "app niet geverifieerd", die je kunt doorklikken).
3. **OAuth-client-ID** van het type *Webapplicatie* met de redirect-URI die Shortcutter toont
   (`https://<jouw-domein>/youtube/callback`). Zet eventueel `PUBLIC_URL` in de compose als de app het domein niet
   goed herkent.
4. Vul client-ID en client-secret in bij API-sleutels.

Beperkingen van Google: video's die vanuit een niet-geaudit API-project worden geüpload, zet YouTube op **privé**
(ook ingeplande). Zet ze in YouTube Studio op openbaar, of vraag een audit aan bij Google. Het gratis quotum is
10.000 eenheden per dag; een upload kost ±1.600, dus ±6 uploads per dag.

## Hoe de view-schatting werkt (en wat de beperkingen zijn)

Views zijn niet te voorspellen. Shortcutter toont daarom altijd een **bereik** met het label
"schatting".

1. De LLM geeft per clip subscores (1-10): hook-sterkte, emotie/verrassing, begrijpelijkheid
   zonder context en afronding.
2. De **viral score** is een gewogen gemiddelde daarvan (35/25/20/20%), licht bijgesteld voor
   een vraag of sterke claim in de eerste 3 seconden, een gezicht in beeld, de duur en het spreektempo.
3. Schatting = kanaalgemiddelde (Instellingen, standaard 1000) × multiplier. Score 5,5 = 1×, elke
   3 punten hoger of lager = factor 3. Het bereik is 0,3× tot 3× het midden, omdat views sterk
   scheef verdeeld zijn.
4. **Kalibratie**: zodra je echte views invult bij geposte clips, past de app zich aan je kanaal aan:
   vanaf 3 clips met de mediaan van echt/geschat, vanaf 20 clips met een lineaire regressie op
   log(views), met het bereik uit de spreiding van je eigen resultaten.

Beperkingen: de score zegt niets over thumbnail, timing, het algoritme of je publiek op dat moment;
de LLM-scores zijn subjectief; met weinig datapunten is de kalibratie onbetrouwbaar. Gebruik de
schatting om clips onderling te vergelijken, niet als belofte.

## Ontwikkeling

```bash
python3.12 -m venv .venv && . .venv/bin/activate
```

```bash
pip install -r requirements-dev.txt
```

```bash
pytest -q
```

Lokaal draaien (ffmpeg moet geïnstalleerd zijn):

```bash
DATA_DIR=./data uvicorn --factory app.main:create_app --reload
```

### Structuur

```
app/
  main.py         FastAPI-app, middleware, /healthz
  core/           config, database, modellen, auth (sessies, CSRF, rate limiting),
                  wachtwoorden (argon2id), setupcode, versleuteling
  api/            routes per onderdeel
  pipeline/       download, transcriptie, fragmentkeuze, framing, ondertitels, render, orchestratie
  estimation/     kenmerken, schatting, kalibratie, YouTube-kanaalgemiddelde
  worker/         wachtrij, worker-thread, opruimen
  web/            templates (Jinja2 + HTMX) en statische bestanden
tests/            pytest
```
