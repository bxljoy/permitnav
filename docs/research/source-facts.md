# Source facts for PermitNav

All facts checked on **2026-11-19** against primary sources (statute text, official agency, court and EU sites, official API specs, robots.txt files). Where a primary source did not answer a question, this is marked **unverified**. This is research, not legal advice.

## Summary

| # | Question | Answer | Confidence | What it means for PermitNav |
|---|---|---|---|---|
| 1 | Copyright on official documents (URL 9 §) | No copyright on statutes, authority decisions, statements by Swedish authorities, or official translations of these. Copyright still applies to maps, fine art, music and poetry inside such documents. Migrationsverket's own terms say **all text on migrationsverket.se may be reused with attribution** (photos and illustrations excluded). Position papers describe themselves as the agency's "uttalanden" (statements), which fits 9 § point 3. | High for statutes, rulings and decisions. High for web-page text (the agency's own licence). Medium for position papers (fits the wording, but no court or preparatory-works source checked). | Statute, ruling and position-paper text can be quoted and committed. Migrationsverket page text is reusable with "Källa: Migrationsverket". Photos and images are not. Third-party reports in Lifos stay protected. |
| 2 | Where position papers are published | Still in **Lifos** (lifos.migrationsverket.se). Each has an HTML metadata page plus a text-based PDF. There is an RSS feed of new and updated papers. No API was found. | High | Use the RSS feed and `/dokument?documentSummaryId=` pages. Do not crawl `/sokning*` (blocked by robots.txt). Store the RS number and version. |
| 3 | Official SFS publication | **svenskforfattningssamling.se** is the official, authentic SFS from 1 April 2018. Each act is a **PDF with an electronic seal**; the HTML page holds only metadata and a link. The PDF contains the in-force date and transitional provisions. There is an RSS feed. No API was found. | High | Parse PDFs (they are text-based). Use the RSS feed to detect new amending acts. For amendments from before April 2018, use the printed-SFS PDF database. |
| 4 | riksdagen.se open data API for SFS | `https://data.riksdagen.se/dokument/sfs-YYYY-NNN.{text,html,json,xml}` returns the full consolidated text, sourced from Regeringskansliet. Each section ends with an amendment marker like "Lag (2026:385)". Versions that are passed but not yet in force appear inline ("/Träder i kraft I:2027-01-01/"), as do repeal dates ("/Upphör att gälla U:2027-01-01/"). Transitional provisions for each amending act are at the end. No per-amendment in-force register; that lives in **SFSR** at rkrattsbaser.gov.se. Terms: free reuse, always cite "Källa: Sveriges riksdag", and do not imply that the Riksdag is behind the service. No rate limit is published. | High | Most of the time metadata can come from one API call. The parser must handle two versions of the same section and the pending and repeal markers. Use SFSR for in-force dates per amending act. |
| 5 | MIG rulings | Published in **Sök rättspraxis** (rattspraxis.etjanst.domstol.se). It has a public JSON REST API ("PUH API", OpenAPI spec) licensed **CC0**, with MIG references, case numbers, cited law sections and the full text as HTML. The old rattsinfosok database is retired, and its robots.txt says `Disallow: /`. | High | Use the API, not scraping. Court code `MIOD`. MIG 2023:1 was confirmed present. |
| 6 | robots.txt and terms | migrationsverket.se blocks e-services, search and some module paths. riksdagen.se sets `crawl-delay: 1` for Claude agents. data.riksdagen.se, svenskforfattningssamling.se, rkrattsbaser.gov.se and rattspraxis.etjanst.domstol.se have no robots.txt (404). domstol.se allows everything. lifos.migrationsverket.se blocks `/sokning*`. | High | Use the APIs and RSS feeds, rate-limit to 1 request/s or slower, and attribute each source as its terms require. |
| 7 | EU AI Act | **Not high-risk under Annex III point 7.** Every item there is limited to systems used "by or on behalf of competent public authorities" or EU bodies. **Article 50(1) applies now** (since 2 Aug 2026): users must be told they are talking to an AI, clearly, at the latest at first interaction. Article 50(2) machine-readable marking of AI-generated text also applies; the Digital Omnibus (Reg. (EU) 2026/1744, in force 27 Jul 2026) gives only systems already on the market before 2 Aug 2026 until 2 Dec 2026. The Omnibus moved Annex III high-risk obligations to 2 Dec 2027. The MIT licence does not exempt an Article 50 system (Art. 2(12)). | High (text read directly). Medium on how Art. 50(2) applies to a system built on a third-party LLM. | Show the AI notice from launch. Add machine-readable marking of answers, for example metadata in the API response and page markup (approach unverified). No high-risk obligations apply. |

---

## 1. Copyright on Swedish official documents

**Source:** Lag (1960:729) om upphovsrätt till litterära och konstnärliga verk, consolidated text "t.o.m. SFS 2026:1120", fetched via https://data.riksdagen.se/dokument/sfs-1960-729.text (same text at https://www.riksdagen.se/sv/dokument-och-lagar/dokument/svensk-forfattningssamling/lag-1960729-om-upphovsratt-till-litterara-och_sfs-1960-729/). Checked 2026-11-19.

**1 kap. 9 §, full text:**

> 9 § Upphovsrätt gäller inte till
>
> 1. författningar,
> 2. beslut av myndigheter,
> 3. yttranden av svenska myndigheter och
> 4. officiella översättningar av sådant som avses i 1-3.
>
> Upphovsrätt gäller dock till verk vilka ingår i en handling som avses i första stycket och är av följande slag:
>
> 1. kartor,
> 2. alster av bildkonst,
> 3. musikaliska verk eller
> 4. diktverk.
>
> Upphovsrätt gäller även till ett verk som ingår i en bilaga till ett beslut av en myndighet, om beslutet avser rätten att ta del av den allmänna handling där verket ingår. Lag (2000:92).

In short:
- No copyright applies to statutes, authority decisions (court judgments are decisions of authorities), statements by Swedish authorities, or official translations of these.
- **Exceptions to the exemption:** maps, works of fine art, musical works and poetry included in such documents stay protected. So does a work in an annex to a decision about access to a public document.

**2 kap. 26 a §** (same source) is the related reuse rule. The second paragraph lets anyone reproduce documents drawn up at Swedish authorities that are *not* covered by 9 § first paragraph:

> Var och en får återge handlingar som är upprättade hos svenska myndigheter men inte är sådana som avses i 9 § första stycket.

The third paragraph excludes the following from that permission: maps, technical drawings, computer programs, "verk som skapats för undervisning", works resulting from scientific research, fine art, musical works, poetry, and works that an authority supplies commercially. Lag (2000:92).

**Applied to PermitNav sources:**

- **Statutes (SFS) and MIGRFS regulations:** "författningar" (statutes), so no copyright. High confidence.
- **MIG rulings and Migrationsverket decisions:** "beslut av myndigheter" (decisions of authorities), so no copyright. High confidence. domstol.se says "Allt material på webbplatsen är skyddat enligt lagen om upphovsrätt" (https://www.domstol.se/om-webbplatsen-och-digitala-kanaler/sa-anvander-du-webbplatsen/). That covers the site's own editorial material and cannot override 9 §. The rulings dataset is in any case published as CC0 (see Q5).
- **Rättsliga ställningstaganden (legal position papers):** Lifos describes them as follows: "Rättsliga ställningstaganden utgör Migrationsverkets uttalanden om hur en författning bör tolkas och är styrande för myndighetens medarbetare. De beslutas av chefen för rättsavdelningen." (https://lifos.migrationsverket.se/dokument?documentSummaryId=50385). They are formally decided statements of a Swedish authority, which matches 9 § point 3 ("yttranden av svenska myndigheter"). Medium-high confidence: preparatory works and case law on what counts as a "yttrande" were **not checked (unverified)**. If 9 § did not apply, 26 a § second paragraph would still allow reproduction, since they are documents drawn up at a Swedish authority and do not fall within the listed exceptions.
- **Migrationsverket web pages (general information text):** whether these are "yttranden" under 9 § is **unverified**. The question matters little in practice, because the agency's own terms grant reuse:
  - Swedish (https://www.migrationsverket.se/om-migrationsverket/om-webbplatsen.html, "Juridisk information → Upphovsrätt"): "All text på vår webbplats är fri att använda och sprida vidare förutsatt att du anger Migrationsverket som källa. Om material från webbplatsen redigeras sker publicering på eget ansvar. Fotografier och illustrationer är skyddade av upphovsrätten och får inte användas utan Migrationsverkets tillstånd."
  - English (https://www.migrationsverket.se/en/about-the-swedish-migration-agency/about-the-website.html): "All text on our website is free to use and distribute provided that you cite the Swedish Migration Agency as the source. If material from the website is edited, publication is at your own risk. Photographs and illustrations are protected by copyright…"
  - The same page asks people not to use the Migrationsverket logo as a link and not to frame the site.
- **Other Lifos documents:** Lifos holds country reports from other organisations, and says some documents are access-restricted for reasons including "upphovsrätt" (https://lifos.migrationsverket.se/om-lifos.html). Third-party documents in Lifos are not covered by 9 § and should not be copied.
- **Open data law:** Lag (2022:818) om den offentliga sektorns tillgängliggörande av data (https://data.riksdagen.se/dokument/sfs-2022-818.text) governs the conditions for reusing data that authorities make available. Under 2 kap. 5 §, conditions must be justified by a public interest and be "objektivt, proportionerligt och icke-diskriminerande". Under 1 kap. 10 § 2, the law does not apply to data that a third party holds copyright in. It does not by itself grant a licence to any particular content. **Unverified:** whether it obliges any of these agencies to publish more than they do.

## 2. Where Migrationsverket's rättsliga ställningstaganden are published

- **Still in Lifos.** The Lifos front page (https://lifos.migrationsverket.se/), checked 2026-11-19, has a section "Aktuellt från Migrationsverkets rättsavdelning" listing recent papers. Examples:
  - 2026-11-03: "Rättsligt ställningstagande. Ordningen för prövningen enligt 21 kap. utlänningslagen (2005:716) - RS/005/2022 (Version 10.0)"
  - 2026-10-16: "RS/013/2026 (version 1.0)"
- Lifos says it is "Migrationsverkets databas för landinformation och rättslig styrning", and that "Migrationsverkets rättsavdelning ansvarar för det rättsliga innehållet" (https://lifos.migrationsverket.se/om-lifos.html).
- migrationsverket.se links to Lifos as its legal-information database and does not host the papers itself (https://www.migrationsverket.se/om-migrationsverket/styrning-och-uppfoljning/lagar-och-forordningar.html). That page also mentions Migrationsverket's own regulations, **MIGRFS**.
- **Formats:**
  - Each paper has an HTML metadata page at `https://lifos.migrationsverket.se/dokument?documentSummaryId=<n>` with title, RS number and version, date ("Upphovsdat"), subject terms, and a note on which version it replaces.
  - The main document is a PDF at `https://lifos.migrationsverket.se/dokument?documentAttachmentId=<n>`, served as `application/octet-stream` with an attachment filename.
  - The PDF checked (RS/005/2022 v10.0) is **text-based**: text extraction worked without OCR.
- **Feed:** RSS for the legal department's news: `http://lifos.migrationsverket.se/2.339945412c72922c2280000/12.39a9cd9514a346077211abc.portlet?state=rss&sv.contenttype=text/xml;charset=UTF-8`. It returned valid RSS 2.0 with the RS titles, dates and links on 2026-11-19. There is a separate RSS feed for country information.
- **API:** none found (**unverified** that none exists).
- **Versioning:** papers are reissued as new versions (e.g. "ersätter version 9.0 som fastställdes 2026-09-17"). PermitNav should key on RS number plus version.
- **Note:** Lifos showed a banner "8/4 2026. Vi utför underhållsarbete" (maintenance). The site worked when checked.

## 3. Svensk författningssamling (SFS): official publication point

- **Confirmed official:** https://svenskforfattningssamling.se/ states: "Här publiceras Svensk författningssamling – den officiella och autentiska versionen av lagar, förordningar och vissa myndighetsföreskrifter. Författningar som har utfärdats före den 1 april 2018 finns i den tidigare tryckta versionen av Svensk författningssamling." Regeringskansliet runs the site.
- From https://svenskforfattningssamling.se/om-svensk-forfattningssamling.html:
  - "Författningarna på denna webbplats anses kungjorda i och med publiceringen."
  - "Författningarna presenteras som de ser ut vid tidpunkten för publicering, dvs. i sin ursprungsversion." The site holds original acts only, not consolidated text.
  - Each act has an "elektronisk stämpel" (electronic seal).
  - For consolidated text and the amendment register ("information om ändringar, ikraftträdanden, hänvisningar till förarbeten"), it points to Regeringskansliets rättsdatabaser (http://rkrattsbaser.gov.se/sfst) and Lagrummet.
  - Acts issued before 2018-04-01 are available as PDFs at http://rkrattsdb.gov.se/sfspdf.
- **Format:**
  - HTML index pages per year (`/regulations/2026/index.html`).
  - One HTML page per act (`/doc/20261686.html`) carrying only metadata (SFS number, title, publication date) and a link to the **PDF** (`/sites/default/files/sfs/2026-08/SFS2026-1686.pdf`). The PDF is the authentic version; no HTML full text was found.
- **In-force dates and transitional provisions:** yes, both are in the PDF. SFS 2026:1686 (Lag om ändring i utlänningslagen) ends with: "1. Denna lag träder i kraft den 1 oktober 2026. 2. Kravet på vistelsetid i 5 kap. 3 i § gäller inte i – ärenden där ansökan om uppehållstillstånd har inkommit till Migrationsverket före ikraftträdandet … 3. I fråga om återkallelse … gäller 7 kap. 3 § i den äldre lydelsen." Footnotes give "Senaste lydelse" (latest previous wording) for each amended section. The PDF text extracted without OCR.
- **Feed:** RSS at https://svenskforfattningssamling.se/regulations/rss.xml/rss.xml (documented at https://svenskforfattningssamling.se/prenumerera.html). Each item carries the title, the link to `/doc/…html`, pubDate, and the SFS number in `dc:creator`. Email subscription is also offered.
- **API:** none found (**unverified** that none exists).
- **Related amendment register (SFSR):** https://rkrattsbaser.gov.se/sfsr?bet=2005:716 lists every amending act with its sections affected ("Omfattning"), "Ikraft" date, an "överg.best." flag, and preparatory works. Example: "Ändring, SFS 2026:1686 … Ikraft: 2026-10-01 överg.best. Förarbeten: Prop. 2025/26:301 …". HTML only. A beta site exists at https://beta.rkrattsbaser.gov.se; its API status was **not checked**. rkrattsbaser.gov.se has no robots.txt (404).

## 4. riksdagen.se open data API for statutes

**Documentation:** https://www.riksdagen.se/sv/dokument-och-lagar/riksdagens-oppna-data/, https://www.riksdagen.se/sv/dokument-och-lagar/riksdagens-oppna-data/dokument/, https://www.riksdagen.se/sv/dokument-och-lagar/riksdagens-oppna-data/anvandarstod/om-apiet/. Checked 2026-11-19.

- **API type:** REST, no key. "Data finns i formaten XML, CSV, JSON, JSONP, text och HTML" (dokument page).
- **Single document:** `https://data.riksdagen.se/dokument/sfs-<year>-<number>.<format>`. All four formats were tested for Utlänningslagen and returned HTTP 200:
  - `.text`: plain text, 429 kB
  - `.html`: HTML, 554 kB
  - `.json`: JSON, 1.0 MB
  - `.xml`: XML, 1.07 MB
- **Search and list:** `https://data.riksdagen.se/dokumentlista/?doktyp=sfs&sok=<query>&utformat=json&sz=<pagesize>&p=<page>`.
  - The response has `@traffar` (hit count) and `@nasta_sida` (next page).
  - Each hit includes `dok_id`, `beteckning`, `titel`, `subtyp: "sfst"`, and `dokument_url_text` / `dokument_url_html`.
  - Query builder: https://data.riksdagen.se/dokumentlista/.
- **JSON/XML metadata** (`dokumentstatus.dokument`):
  - `dok_id`, `beteckning` ("2005:716"), `titel`, `subtitel` ("t.o.m. SFS 2026:1752", meaning amended up to that number), `organ`, `datum`, `publicerad`, `systemdatum`, and `text` (the whole consolidated text in one string).
  - `dokuppgift` adds `utfardad` (date issued), `andrattillochmed` and `text2` (corrigenda, e.g. "Rättelseblad 2009:16 … har iakttagits").
  - The text header says "Källa: Regeringskansliet / Lagrummet". The consolidated text comes from Regeringskansliet; the riksdagen.se page links "Källa: Fulltext (Regeringskansliet)" (http://rkrattsbaser.gov.se/sfst?bet=2005:716) and "Ändringsregister: SFSR (Regeringskansliet)".
- **How consolidated text is represented** (observed in `sfs-2005-716.text`):
  - Plain text with chapter headings ("1 kap. …"), section starts ("2 § …"), and paragraphs split by blank lines.
  - **Amendment marker per section:** the section's last amending act is appended, e.g. "… Lag (2026:385)." and "Lag (2022:303)." Sections never amended carry no marker. Repealed sections read e.g. "3 e § Har upphävts genom lag (2019:180)."
  - **Pending and expiring versions are both included, labelled:**
    ```
    12 § /Upphör att gälla U:2027-01-01/
    Ett tidsbegränsat uppehållstillstånd får beviljas en utlänning som behöver vård enligt lagen (1990:52) …

    12 § /Träder i kraft I:2027-01-01/
    Ett tidsbegränsat uppehållstillstånd får beviljas en utlänning som behöver omhändertas enligt lagen (2026:1600) … Lag (2026:1619).
    ```
    Other variants seen: "/Träder i kraft I:den dag som regeringen bestämmer/", "/Rubriken träder i kraft I:…/", and "/Upphör att gälla U:2028-01-01 genom lag (2026:1687)./".
  - **Transitional provisions:** a final "Övergångsbestämmelser" block, grouped by SFS number of each amending act ("2026:1686 / 1. Denna lag träder i kraft den 1 oktober 2026. 2. …").
  - **Not included:** a per-amendment in-force register for amendments already in force. Use SFSR for that (Q3). A section's past in-force date cannot be read from the consolidated text alone.
- **Terms of use** (https://www.riksdagen.se/sv/dokument-och-lagar/riksdagens-oppna-data/anvandarstod/anvandningsvillkor/):
  - "Riksdagens öppna data är fria att användas och spridas vidare så länge du anger källa." Attribution format: "Källa: Sveriges riksdag" / "Source: The Swedish Parliament".
  - Services must make it clear that the Riksdag is not behind them, and may not use the Riksdag logo or the name "Sveriges riksdag". Using the word "riksdagen" as part of a name is allowed.
  - Copyright-protected material (e.g. images) is excluded, and GDPR applies to personal data.
  - **No named licence** (CC0 or similar) is stated.
- **Rate limits:** none published on the pages checked (**unverified**). data.riksdagen.se has no robots.txt (404).
- **Roadmap check:** the roadmap says the consolidated text is "a convenience version, not the official one". That is consistent with svenskforfattningssamling.se, which says SFS publishes acts "i sin ursprungsversion", and with the consolidated text being sourced from Regeringskansliet's database.

## 5. Migrationsöverdomstolen guiding rulings (MIG)

- **Current home: Sök rättspraxis**, https://rattspraxis.etjanst.domstol.se/sok/.
  - Migrationsöverdomstolen's own page: "I söktjänsten Sök rättspraxis hittar du Migrationsöverdomstolens vägledande avgöranden från 2017. Där finns också referat sedan 2007." (https://www.domstol.se/migrationsoverdomstolen/avgoranden/)
  - The service page says it has "rättsfallsreferat, domar och beslut från de högsta instanserna", with new rulings added from 4 March 2025, summaries (referat) from 1981, and Migrationsöverdomstolen from 2006. RSS subscription is available. (https://www.domstol.se/tjanster-och-blanketter/sok-rattspraxis/)
- **Old database retired:** rattsinfosok.domstol.se redirects to https://www.domstol.se/tjanster-och-blanketter/sok-rattspraxis/databasen-vagledande-avgoranden/, which says "Databasen Vägledande avgöranden har flyttat". Its robots.txt is `User-agent: * / Disallow: /`.
- **API:** "Finns rättspraxis som öppna data? Ja" (https://www.domstol.se/om-webbplatsen-och-digitala-kanaler/oppna-data/).
  - The OpenAPI spec is at https://rattspraxis.etjanst.domstol.se/openapi/puh-openapi.yaml ("PUH API", v1).
  - Endpoints:
    - `GET /api/v1/domstolar`: list of courts. Migrationsöverdomstolen is `MIOD`.
    - `GET /api/v1/publiceringar?domstolkod=MIOD&publiceringstyper=…&publicerad_fran_och_med=YYYY-MM-DD&page=&pagesize=`: total count in the `x-total-count` header.
    - `GET /api/v1/publiceringar/{id}`
    - `GET /api/v1/publiceringar/grupp/{id}`
    - `POST /api/v1/sok`
    - `GET /api/v1/sokforfiningar`
    - `GET /api/v1/bilagor/{lagringId}`: attachments.
  - Format: JSON. Each record has:
    - `referatNummerLista` (e.g. `["MIG 2026:11"]`), `malNummerLista`, `avgorandedatum`, `publiceringstid`, `typ` (e.g. `PREJUDIKAT`, `VAGLEDANDE_MEN_EJ_PREJUDICERANDE`), `publiceringsform` (`REFERAT`), `sammanfattning`, `ecliNummer`
    - `innehall`: the full text as **HTML**
    - `lagrumLista`: e.g. `{"referens": "5 kap. 3 a § … utlänningslagen", "sfsNummer": "2005:716"}`
    - `forarbeteLista`, `hanvisadePubliceringarLista` (cited rulings, e.g. MIG 2024:14), `europarattsligaAvgorandenLista`, `bilagaLista`
  - Tested 2026-11-19:
    - `domstolkod=MIOD` returned `x-total-count: 535`.
    - `pagesize=600` returned only 100 records, so the page size appears capped at 100 (observed, not documented).
    - **MIG 2023:1** is present (case UM 3049-22, decided 2023-01-19).
    - The `publiceringstyper` filter value `referat` returned 0 hits. Filter on `publiceringsform` or client-side instead.
- **Licence:** the Sveriges dataportal record for the data service `https://rattspraxis.etjanst.domstol.se/api/v1/publiceringar` ("Rättspraxis från Sveriges Domstolar", publisher Domstolsverket) gives **CC0 1.0** (http://creativecommons.org/publicdomain/zero/1.0/). Metadata was fetched from https://admin.dataportal.se/store/601/metadata/3755; the human page is https://www.dataportal.se/datasets/601_3755.
- **Rate limits:** none documented (**unverified**). rattspraxis.etjanst.domstol.se has no robots.txt (404).
- **Older PDFs:** some referat PDFs remain on domstol.se, e.g. https://www.domstol.se/globalassets/filer/domstol/migrationsoverdomstolen/avgoranden/2017/referat-i-mal-nr-um-3926-16.pdf. They are not needed when the API is used.
- **Lagrummet:** Domstolsverket runs lagrummet.se as the public legal-information portal linking to these sources (https://www.domstol.se/om-webbplatsen-och-digitala-kanaler/lagrummet-se-och-rattsinformationssystemet/). No separate MIG API was found there.

## 6. robots.txt and terms of use

All robots.txt files were fetched 2026-11-19.

| Site | robots.txt | Terms and reuse |
|---|---|---|
| www.migrationsverket.se | See the block below. Sitemap: https://www.migrationsverket.se/sitemapindex.xml | Text free to use and redistribute with "Migrationsverket" cited as source. Photos and illustrations need permission. No logo as link, no framing. (https://www.migrationsverket.se/om-migrationsverket/om-webbplatsen.html) |
| lifos.migrationsverket.se | Same base rules as the main site plus `User-agent: * Disallow: /sokning*` ("Migv Added 20260415"). | No separate terms page found (**unverified**). Third-party documents may be copyright-restricted (om-lifos page). |
| www.riksdagen.se | `sitemap: https://www.riksdagen.se/sitemapindex0.xml.gz`; `user-agent: Claudebot`, `Claude-User` and `Claude-SearchBot` each with `crawl-delay: 1`. No Disallow rules. | Open data: free use with "Källa: Sveriges riksdag", no implied endorsement, no logo or name "Sveriges riksdag" (anvandningsvillkor page above). |
| data.riksdagen.se | 404 (no file) | As riksdagen.se open data. |
| svenskforfattningssamling.se | 404 (no file) | Only a privacy policy found (https://svenskforfattningssamling.se/om-webbplatsen.html). No reuse licence stated. Content is statutes, so 9 § applies. |
| rkrattsbaser.gov.se (SFSR) | 404 (no file) | Not checked (**unverified**). Content is statutes and register data. |
| www.domstol.se | `User-agent: * Allow: /`; Sitemap https://www.domstol.se/sitemap.xml | "Allt material på webbplatsen är skyddat enligt lagen om upphovsrätt. Citera oss gärna men ange källa." No logo as link, no framing. (https://www.domstol.se/om-webbplatsen-och-digitala-kanaler/sa-anvander-du-webbplatsen/) Rulings themselves: 9 §. |
| rattspraxis.etjanst.domstol.se | 404 (no file) | Data service CC0 (dataportal record above). |
| rattsinfosok.domstol.se (retired) | `User-agent: * Disallow: /` | n/a |
| eur-lex.europa.eu | Returned an AWS WAF challenge (HTTP 202, empty) to curl and WebFetch. The AI Act text was read through the EU Publications Office Cellar (`http://publications.europa.eu/resource/celex/32024R1689`). | Not checked. |

migrationsverket.se robots.txt, relevant lines:

```
User-agent: *
Disallow: /*.html.printable
Disallow: /*?sv.state=*
Disallow: /*.pdf?properties=*
Disallow: /*;jsessionid=*
... (bookmark, image, cart, profiling, keepAlive, contact query patterns)
Disallow: /lankade-moduler/
Disallow: /lankade-moduler-engelska/
Disallow: /webbfunktion/
Disallow: /e-tjanster/
Disallow: /ovrigt/sok*
Disallow: /ovrigt/search*
User-agent: RekAiBot  (also) Disallow: /*/flikar/
```

Normal content pages and PDFs (without `?properties=`) are allowed.

**PSI and open data law (2022:818):** see Q1. It sets limits on the conditions authorities may attach to reuse. It does not itself license any specific content.

## 7. EU AI Act (Regulation (EU) 2024/1689)

**Text read from the EU Publications Office (Cellar)**, Official Journal L 2024/1689 of 12.7.2024: http://publications.europa.eu/resource/celex/32024R1689 (canonical ELI: http://data.europa.eu/eli/reg/2024/1689/oj; EUR-Lex: https://eur-lex.europa.eu/eli/reg/2024/1689/oj). Checked 2026-11-19.

**Annex III point 7, verbatim:**

> 7. Migration, asylum and border control management, in so far as their use is permitted under relevant Union or national law:
> (a) AI systems intended to be used by or on behalf of competent public authorities or by Union institutions, bodies, offices or agencies as polygraphs or similar tools;
> (b) AI systems intended to be used by or on behalf of competent public authorities or by Union institutions, bodies, offices or agencies to assess a risk, including a security risk, a risk of irregular migration, or a health risk, posed by a natural person who intends to enter or who has entered into the territory of a Member State;
> (c) AI systems intended to be used by or on behalf of competent public authorities or by Union institutions, bodies, offices or agencies to assist competent public authorities for the examination of applications for asylum, visa or residence permits and for associated complaints with regard to the eligibility of the natural persons applying for a status, including related assessments of the reliability of evidence;
> (d) AI systems intended to be used by or on behalf of competent public authorities, or by Union institutions, bodies, offices or agencies, in the context of migration, asylum or border control management, for the purpose of detecting, recognising or identifying natural persons, with the exception of the verification of travel documents.

**Assessment:**
- Every sub-point requires use "by or on behalf of competent public authorities" or EU bodies. A free public information chatbot run by a private individual, not used by or for an authority, falls outside point 7.
- It also does none of the listed functions: no risk assessment, no examination of applications, no identification.
- Under Article 6(2), only Annex III systems are high-risk by that route. **Conclusion: not high-risk under Annex III point 7.** High confidence on the text.
- Caveat: if an authority (or a lawyer acting for one) later used PermitNav to assist in examining applications, point 7(c) could apply to that use. The Omnibus did not amend Annex III point 7 (no amendment to it found in Reg. 2026/1744).

**Article 50, relevant paragraphs, verbatim:**

> 1. Providers shall ensure that AI systems intended to interact directly with natural persons are designed and developed in such a way that the natural persons concerned are informed that they are interacting with an AI system, unless this is obvious from the point of view of a natural person who is reasonably well-informed, observant and circumspect, taking into account the circumstances and the context of use. …
>
> 2. Providers of AI systems, including general-purpose AI systems, generating synthetic audio, image, video or text content, shall ensure that the outputs of the AI system are marked in a machine-readable format and detectable as artificially generated or manipulated. … This obligation shall not apply to the extent the AI systems perform an assistive function for standard editing or do not substantially alter the input data provided by the deployer or the semantics thereof …
>
> 4. (second subparagraph) Deployers of an AI system that generates or manipulates text which is published with the purpose of informing the public on matters of public interest shall disclose that the text has been artificially generated or manipulated. This obligation shall not apply … where the AI-generated content has undergone a process of human review or editorial control and where a natural or legal person holds editorial responsibility for the publication of the content.
>
> 5. The information referred to in paragraphs 1 to 4 shall be provided to the natural persons concerned in a clear and distinguishable manner at the latest at the time of the first interaction or exposure. The information shall conform to the applicable accessibility requirements.

What this means for a chatbot:
- **50(1)** applies to the provider. PermitNav's author places the system into service under its own name, free of charge, which fits the definition of "provider" in Art. 3(3): "whether for payment or free of charge". Users must be told they are interacting with an AI, clearly, at first interaction, in an accessible way (50(5)).
- **50(2)** asks providers of systems generating synthetic text to mark outputs in a machine-readable, detectable way. Open points (**unverified**, no official guidance read):
  - Whether a RAG app built on a third-party general-purpose model must add its own marking, or can rely on the model provider's.
  - What marking is "effective" for plain text.
  - The Omnibus revised Art. 50(7): codes of practice on "detection, marking and labelling".
- **50(4) second subparagraph** covers text "published" to inform the public on matters of public interest. Answers shown to one user in a chat are arguably not "published". **Unverified.** The roadmap's planned public "what changed" log and blog post are human-edited, which falls under the editorial-control exemption if the author reviews them.
- **Art. 2(12):** "This Regulation does not apply to AI systems released under free and open-source licences, unless they are placed on the market or put into service as high-risk AI systems or as an AI system that falls under Article 5 or 50." The MIT licence therefore does **not** remove the Article 50 duties.
- **Art. 3(4):** a "deployer" excludes "personal non-professional activity". A public service offered to others is not purely personal use (interpretation).
- **Art. 4 (AI literacy):** applies from 2 Feb 2025. The Omnibus replaced it with a softer "take measures to support the development of AI literacy … does not require … any specific level". The impact for a solo project is minimal.

**Dates (Article 113, original text):** "It shall apply from 2 August 2026." Chapters I and II apply from 2 Feb 2025. Chapter V (GPAI) and others apply from 2 Aug 2025. Article 6(1) applies from 2 Aug 2027. Article 50 is in Chapter IV and so follows the **general date, 2 August 2026**, which is already past.

**Later amendment: Digital Omnibus on AI, Regulation (EU) 2026/1744** of 8 July 2026, OJ L of 24.7.2026 (http://publications.europa.eu/resource/celex/32026R1744). Read directly:
- **Entry into force:** "on the third day following that of its publication", i.e. 27 July 2026.
- **High-risk dates moved.** Article 113(c) now reads: Chapter III Sections 1–3 apply from "2 December 2027 as regards AI systems classified as high-risk pursuant to Article 6(2) and Annex III" and "2 August 2028 as regards … Article 6(1) and Annex I".
- **Article 50 not postponed in general.** A new Art. 111(4) reads: "Providers of AI systems, including general-purpose AI systems, generating synthetic audio, image, video or text content, that have been placed on the market before 2 August 2026 shall take the necessary steps in order to comply with Article 50(2) by 2 December 2026." Recital: "a transitional period of four months for providers who have already placed their systems on the market before the 2 August 2026". PermitNav will launch after 2 Aug 2026, so Art. 50(1) and 50(2) apply from launch.
- **Other changes:** Art. 4 (AI literacy) replaced; Art. 50(7) (codes of practice) replaced; some Art. 5 prohibitions added from 2 Dec 2026; Articles 102–110 apply from 27 July 2026.
- **Not checked:** whether any further amendment, corrigendum or Commission guidance on Article 50 was published after 2026/1744 (**unverified**; EUR-Lex was blocked by a WAF challenge). The separate "Digital Omnibus" on data and GDPR was not researched.

---

## Changes needed in ROADMAP.md

Sources section:

1. **Utlänningslagen row ("riksdagen.se, which has an open data API for statutes"):** add the concrete endpoint `https://data.riksdagen.se/dokument/sfs-2005-716.{text,json,xml,html}` and the list endpoint `https://data.riksdagen.se/dokumentlista/?doktyp=sfs&utformat=json`. Note that the consolidated text comes from Regeringskansliet ("Källa: Regeringskansliet / Lagrummet").
2. **Amending laws row:** replace "(confirm this is still the official publication point)" with: "Confirmed official and authentic from 1 April 2018. One sealed PDF per act (the HTML page is metadata only). RSS feed at /regulations/rss.xml/rss.xml. No API. Acts before April 2018: printed-SFS PDFs at rkrattsdb.gov.se/sfspdf."
3. **Add a row: SFSR amendment register** (https://rkrattsbaser.gov.se/sfsr?bet=2005:716, Regeringskansliet, Swedish). It lists every amending act with the sections affected, its in-force date ("Ikraft") and a transitional-provisions flag. This is the cleanest source for `valid_from` per amendment. HTML only.
4. **Position papers row:** replace "migrationsverket.se (check where they are published now…)" with: "Still in Lifos (lifos.migrationsverket.se). HTML metadata page `/dokument?documentSummaryId=` plus text-based PDF. RSS feed 'Aktuellt från Migrationsverkets rättsavdelning'. Versioned (RS/005/2022 version 10.0). robots.txt disallows `/sokning*`, so discover new papers through RSS, not search pages."
5. **MIG rulings row:** replace "Swedish courts' published guiding rulings" with: "Sök rättspraxis (rattspraxis.etjanst.domstol.se). Open JSON API (OpenAPI spec at /openapi/puh-openapi.yaml), court code MIOD, CC0 licence. Full text as HTML, plus cited SFS sections and cited rulings. The old rattsinfosok database is retired and its robots.txt blocks all crawling."
6. **Optional new row: MIGRFS** (Migrationsverket's own regulations). These are binding regulations below ordinances in the hierarchy, free of copyright as "författningar", and listed from https://www.migrationsverket.se/om-migrationsverket/styrning-och-uppfoljning/lagar-och-forordningar.html.
7. **Paragraph "The consolidated statute text on riksdagen.se is a convenience version…":** keep it, but add:
   - The API text already contains not-yet-in-force versions ("/Träder i kraft I:YYYY-MM-DD/") and expiring versions ("/Upphör att gälla U:YYYY-MM-DD/"), so one section number can appear twice.
   - It already contains the transitional provisions grouped by amending SFS number at the end.
   - It does not give past in-force dates per amendment; take those from SFSR or the SFS PDF.
   - Phase 1 metadata (`status`, `valid_from`, `valid_to`) and Phase 4 "upcoming changes" can be filled largely from these markers. The parser must handle them.
8. **"Crawling manners" paragraph:** add the concrete rules:
   - Prefer the APIs and RSS feeds over crawling (riksdagen API, SFS RSS, Lifos RSS, rättspraxis API).
   - riksdagen.se asks Claude agents for `crawl-delay: 1`. Use 1 request/s or slower everywhere.
   - migrationsverket.se disallows `/e-tjanster/`, `/webbfunktion/`, `/lankade-moduler*/`, `/ovrigt/sok*` and `*.html.printable`. Use its sitemap at /sitemapindex.xml.
   - Lifos disallows `/sokning*`.
9. **Add an attribution line** to the Sources section and the app footer: "Källa: Sveriges riksdag" for riksdagen open data, and "Källa: Migrationsverket" for Migrationsverket text. Do not use the Riksdag's or Migrationsverket's names or logos in a way that implies endorsement. The riksdagen terms explicitly forbid using "Sveriges riksdag" in a service's name or implying it is behind the service.

Repo rules section:

10. **"Ship the crawler, not the crawled content" bullet:** replace the copyright sentence with:
    - Statutes, MIGRFS, court rulings and authority decisions have no copyright (upphovsrättslagen 1 kap. 9 §). Position papers are agency statements and very likely covered too.
    - Migrationsverket's own terms make all **text** on migrationsverket.se free to reuse with attribution. Photos and illustrations are not.
    - Third-party documents in Lifos (other organisations' country reports) are protected.
    - Keep raw HTML and PDFs out of the repo anyway, for size, freshness and privacy. Test fixtures may include statute, ruling and Migrationsverket text excerpts, with source and fetch date recorded.
11. **Licence bullet:** add that MIT licensing does not exempt the app from AI Act Article 50 (Art. 2(12)).

Elsewhere (outside Sources and Repo rules, for the author's information):

12. **Phase 6 Safety:** add the AI Act duties:
    - PermitNav is **not** high-risk under Annex III point 7, because that point covers only use by or on behalf of public authorities.
    - **Article 50(1)** has applied since 2 Aug 2026. Show the "you are talking to an AI" notice clearly at first interaction, in an accessible way.
    - **Article 50(2)** requires machine-readable marking of generated answers, which the Omnibus did not postpone for new systems. Decide how (e.g. a flag in API responses and page metadata) before the public beta.
    - Record these as a requirement in phase 6, not just a notice.

## Historical versions of statutes (checked 2026-11-19)

Question: where can past consolidated wordings of a section ("which wording applied on date X") be obtained from official sources?

**Short answer:** no official source found publishes past consolidated versions for download. The authentic amending acts (SFS PDFs) plus the SFSR register are enough to rebuild them. Each amending act prints the **new** wording of every section it touches. A footnote names the act that gave the **previous** wording ("Senaste lydelse 2013:606"). The act does not print the old text itself.

### 1. riksdagen.se / data.riksdagen.se: current text only

- `https://data.riksdagen.se/dokumentstatus/sfs-2005-716.json` has no version, revision or history fields. `dokument` holds `dok_id`, `subtitel` ("t.o.m. SFS 2026:1752"), `publicerad` / `systemdatum` (2026-10-02) and the one current `text`. `dokuppgift` holds only `artal`, `utfardad`, `text2`, `andrattillochmed` and `utdrag`. Adding `?version=1` returns the same 1.0 MB document.
- `https://data.riksdagen.se/dokumentlista/?doktyp=sfs&sok=2005:716&utformat=json` returns 141 hits. Each is a separate base statute (`sfs-2005-716`, `sfs-2016-752`, …). There are no older consolidations of 2005:716 and no amending acts.
- Amending acts are not documents in this API: `https://data.riksdagen.se/dokumentstatus/sfs-2026-1686.json` returns 404. Guessed versioned paths such as `https://data.riksdagen.se/dokument/sfs-2005-716/1.text` also return 404.
- The bulk dataset catalogue `https://data.riksdagen.se/dataset/katalog/dataset.xml` has no SFS dataset. Its types are anforande, bet, ds, prop, sou, votering etc.; zero entries contain "sfs".
- The human page `https://www.riksdagen.se/sv/dokument-och-lagar/dokument/svensk-forfattningssamling/utlanningslag-2005716_sfs-2005-716/` has no "tidigare lydelse" or version selector. It links only to SFST and SFSR at rkrattsbaser.gov.se.
- **Useful indirect source on riksdagen:** government bills (propositioner) contain the draft amending act as a two-column table, "Nuvarande lydelse | Föreslagen lydelse". This prints the old wording next to the proposed wording. Example: Prop. 2024/25:18 (`https://data.riksdagen.se/dokument/HC0318.text`) has 15 such tables. It also contains the same enacting clause as SFS 2024:1220.
  - Caveats: the "Nuvarande lydelse" column shows the wording when the bill was written, and the riksdag committee can change the final text. Treat it as a cross-check, not as the source.

### 2. Regeringskansliets rättsdatabaser (SFST): current text only

- `https://rkrattsbaser.gov.se/sfst?bet=2005:716` shows one consolidated text, "Ändring införd: t.o.m. SFS 2026:1752".
- The advanced search form `https://rkrattsbaser.gov.se/sfst/adv` has four inputs: `fritext`, `sbet`, `rub`, `org`. It has one checkbox, "Visa endast gällande författningar". There is no date or version option.
- Lagrummet describes SFST as "lagar och förordningar i den senaste versionen med alla ändringar införda i den ursprungliga texten" (`https://www.lagrummet.se/rattskalla/`).
- The EU Publications Office's Sweden page says the consolidation is done by the Regeringskansliet secretariat, that "All legislation in force is available in a consolidated version", and that "Consolidation is for information purposes only". It does not mention historical versions (`https://op.europa.eu/en/web/forum/sweden-oj`).
- **Beta site** `https://beta.rkrattsbaser.gov.se/`: a JavaScript single-page app whose front end (`/dist/main-client.js`) queries an internal Elasticsearch proxy at `/elasticsearch/SearchEsByRawJson`. The SFS tabs are `grundforfattning`, `andringsforfattningar` and `forfattningstext`, with no version tab.
  - The record for 2005:716 (index `rattsinformationsfsprod-20261119`) has the fields `previousId` (66023), `historisk: false` and `arkiverad: false`. Its `fulltext` object also has `historisk` / `arkiverad` flags. This *hints* that earlier consolidations exist internally.
  - **Unverified:** whether past consolidations can be retrieved. I did not query the undocumented endpoint further, because it is an internal, undocumented interface rather than a published API.
  - The same record's `andringsforfattningar` array (124 entries) gives, per amending act, `beteckning`, `anteckningar` (sections affected), `ikraftDateTime`, `ikraftOvergangsbestammelse` and `forarbeten`. This is the same content as SFSR, in JSON.

### 3. Amending acts: new wording per section, previous wording by reference only

Checked SFS 2024:1220, "Lag om ändring i utlänningslagen (2005:716)", issued 5 December 2024, in force 1 January 2025. Page: `https://svenskforfattningssamling.se/doc/20241220.html`. PDF: `https://svenskforfattningssamling.se/sites/default/files/sfs/2024-12/SFS2024-1220.pdf`.

- **Enacting clause in "dels att" form**, with machine-parseable operations:
  - "dels att 6 a kap. 7 och 8 §§ ska upphöra att gälla"
  - "dels att rubriken närmast före 6 a kap. 7 § ska utgå"
  - "dels att nuvarande 6 a kap. 9 och 10 §§ ska betecknas 6 a kap. 8 och 9 §§" (renumbering)
  - "dels att 5 kap. 18 a §, … 12 kap. 4 § … ska ha följande lydelse"
  - "dels att det ska införas elva nya paragrafer, 6 a kap. 3 a–3 c, …"
- **Body:** the full new wording of each changed or new section, grouped under its chapter heading ("5 kap. / 18 a § …").
- **The previous wording is not printed.** Each amended section has a footnote naming the act that gave its wording until now, e.g. "Senaste lydelse 2018:319", "Senaste lydelse 2013:606", "Senaste lydelse av tidigare 9 § 2013:606". Sometimes the footnote adds a short remark, e.g. "Ändringen innebär bl.a. att andra stycket tas bort." Sections never amended before have no footnote, so the previous wording is the original act's.
- **In force:** "Denna lag träder i kraft den 1 januari 2025."
- The PDF text extracts without OCR, but needs a real PDF text extractor. The text streams are FlateDecode.
- The SFSR "Omfattning" line for the same act gives the same operations in short form: "upph. 6 a kap. 7, 8 §§ …; nuvarande 6 a kap. 9, 10 §§ betecknas 6 a kap. 8, 9 §§; ändr. …" (`https://rkrattsbaser.gov.se/sfsr?bet=2005:716`).
- **Pre-2018 acts:** these are needed when a "Senaste lydelse" footnote points before April 2018, e.g. 2013:606.
  - Lagrummet says `https://rkrattsdb.gov.se/sfspdf/` holds printed-SFS PDFs "från och med SFS 1998:306 till och med SFS 2018:159" (`https://www.lagrummet.se/rattskalla/`).
  - On 2026-11-19 that host presented an **expired TLS certificate**: WebFetch failed with "certificate has expired" and curl with exit 60. Its PDF URL pattern and content are **unverified**.
  - **Update 2026-11-23:** the certificate is valid again. The PDF pattern is `https://rkrattsdb.gov.se/SFSdoc/<yy>/<yy><nnnn>.PDF` (e.g. `/SFSdoc/13/130606.PDF` for 2013:606); text extracts without OCR. No robots.txt. lagen.nu keeps a consolidation after each amending act at `https://lagen.nu/2005:716/konsolidering/<act>` (robots.txt allows content pages), used as an independent check.

**How reconstruction works**

1. Start from today's consolidated text (riksdagen API or SFST), split into sections.
2. For each amending act in SFSR with an in-force date after the target date, newest first:
   - Each section the act changed or added gets `valid_from = Ikraft`.
   - The wording before it is the text of that section in the act named by its "Senaste lydelse" footnote. If there is no footnote, it is the base act's original wording.
   - Get that act's PDF and take the section text: for 2018-04 onward from svenskforfattningssamling.se, before that from rkrattsdb.gov.se/sfspdf.
   - The previous version's `valid_to` is the day before the new `Ikraft`.
3. Undo renumberings ("betecknas"), repeals ("upphöra att gälla") and heading moves using the enacting clause.
4. Handle the special cases:
   - Acts with several in-force dates or "den dag regeringen bestämmer": the date comes from a separate ikraftträdandeförordning, which is also listed in SFSR (e.g. 2023:56 brings 2021:1189 into force).
   - Rättelseblad (corrigenda).
   - Expiring and temporary wordings.

Reconstruction can also run forward: the original act plus every amending act in order.

**Checks:**

- Every rebuilt "current" version must match today's consolidated text.
- The "Lag (YYYY:NNN)" markers in the consolidated text must match the latest SFSR act per section.
- Cross-check against the "Nuvarande lydelse" column in the bill.

### 4. Other point-in-time sources

- **Lagrummet** (Domstolsverket) is a link portal. For SFS it points to SFSR, SFST, rkrattsdb.gov.se/sfspdf and svenskforfattningssamling.se, with no historical-version service of its own (`https://www.lagrummet.se/rattskalla/`).
- **N-Lex** (EU) has a Swedish search form with a "Validity" date type (`https://n-lex.europa.eu/n-lex/legis_sv/sfs.fulltext_form?lang=en`). It is **unverified** whether this returns point-in-time text; it likely searches the same current SFST data.
- **ELI:** no evidence found that Sweden publishes ELI point-in-time URIs for SFS (**unverified**). The OP Sweden page does not mention ELI.
- **Unofficial (not primary):** lagen.nu keeps past consolidations per amending act at URLs like `https://lagen.nu/1976:725/konsolidering/2013:177`, marked "Inaktuell version", with Regeringskansliets rättsdatabaser named as the source. Whether it has the same for 2005:716 and 2006:97 is unverified. It could serve as a cross-check, never as the source of record.

### 5. Size of a reconstruction back to 2021-01-01

Counted from SFSR, `https://rkrattsbaser.gov.se/sfsr?bet=2005:716` and `https://rkrattsbaser.gov.se/sfsr?bet=2006:97`. The count includes every entry numbered 2021 or later, or with an Ikraft date on or after 2021-01-01. No pre-2021 act had a later in-force date.

| Statute | SFSR entries | Of which ikraftträdandeförordningar (no text) | Text-amending acts | Acts with renumbering ("betecknas") |
|---|---|---|---|---|
| Utlänningslagen (2005:716) | 51 (2021:223 … 2026:1752) | 4 (2023:56, 2023:569, 2025:850, 2026:1752) | **47** | 8 |
| Utlänningsförordningen (2006:97) | 45 (2021:224 … 2026:1746) | 1 (2023:58) | **44** | 4 |

- About 90 amending acts in total, almost all post-2018 sealed PDFs on svenskforfattningssamling.se.
- Several are not yet in force, e.g. utlänningslagen 2026:1427 (2027-12-01), 2026:1619 (2027-01-01) and 2026:1687 (2028-01-01). The riksdagen text already carries these as "/Träder i kraft/" versions.
- The "Senaste lydelse" footnotes will also point to some pre-2018 acts (e.g. 2013:606, 2014:198). Their wording is needed only as the version valid on 2021-01-01 for sections amended since then.
- The counts come from a scripted parse of the SFSR HTML. Exact figures may shift by one or two when checked by hand.
