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
- **Posttekst**: de LLM schrijft per clip een titel, beschrijving en hashtags om te kopiëren (standaard in het
  Engels voor meer bereik; te wijzigen bij Instellingen). Voor bestaande clips: "Posttekst voor alle clips".
- **Beeld niet gevuld (16:9 klein in 9:16)**: kies bij Beeldvulling "Vullen" (per project, als standaard bij
  Instellingen, of per clip met een schuif voor de positie). "Automatisch" vult alleen als er een gezicht in beeld is.
- **Render mislukt**: de ruwe ge-cropte versie en de `.ass` blijven downloadbaar; klik op *Opnieuw renderen*.
- **yt-dlp verouderd**: elke image-build haalt de nieuwste yt-dlp. Start de workflow handmatig
  (Actions → Docker image → Run workflow) en doe daarna `docker compose pull && docker compose up -d`.

### Opruimen

Bij Instellingen (admin) stel je in na hoeveel dagen bronbestanden van afgeronde projecten
verwijderd worden (standaard 7; clips blijven bewaard) en optioneel hele projecten. Je ziet daar
ook het schijfgebruik per project.

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
