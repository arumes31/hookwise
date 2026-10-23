# Forwarding

Unter **Forwarding → Webhook anlegen** lässt sich eine ausgehende
Alarmierung konfigurieren. Die weitere Alarmierungskette betreibt der Kunde.

1. Namen, CW Company ID und den eingehenden Quell-Endpoint auswählen.
2. Die öffentlich erreichbare HTTPS-Ziel-URL des Kunden eintragen (Port 443).
3. Optional den Score-Filter aktivieren und das numerische Score-Feld sowie den Mindestscore festlegen, z. B. `$.score`
   und `70`. Gleichheit ist eingeschlossen. Für CIPP-Batches ist beispielsweise
   `$.Results[*].Score` möglich, sofern die tatsächliche Payload dieses Feld enthält.
   CIPP Defender Incidents enthalten häufig nur `Severity` und keinen Score.
   Für diese Meldungen den Score-Filter deaktivieren; ohne weitere Filter wird das gesamte
   kundenzugeordnete Alarmobjekt weitergeleitet, `score` und `minimum_score` sind `null`.
4. Bei Bedarf einen Bearer-Token und/oder einen gemeinsamen HMAC-Schlüssel
   hinterlegen und speichern.
5. Im Feld **Kommentar / interne Notizen** können Ansprechpartner, die
   zuständige Alarmierungskette oder Übergabehinweise dokumentiert werden
   (maximal 2000 Zeichen). Diese Notiz erscheint intern in der Übersicht,
   wird im Konfigurationsbackup gesichert und nicht an den Empfänger gesendet.
   Zugangsdaten gehören in die dafür vorgesehenen Felder.

Der Score-Pfad muss zur tatsächlich eingehenden JSON-Meldung passen. Es gibt
keine automatische Umrechnung von Defender-Severity (`high`, `medium` usw.)
in einen numerischen Score. Fehlende, negative, unendliche, boolesche oder
nicht numerische Werte lösen bei aktivem Score-Filter keine Zustellung aus.

## Schweregrad statt numerischem Score

Für Defender Incidents lässt sich im Connector **Nach Schweregrad filtern (Severity)**
aktivieren. Als Feld `$.Results[*].Severity` und als Mindest-Schweregrad **High**
wählen. Der numerische Score-Filter bleibt aus, wenn die Quelle keinen Score liefert.
Damit werden `informational` (etwa EICAR), `low` und `medium` nicht an die Bereitschaft
geschickt. Ab **Medium** werden Medium und High zugelassen. Defender kennt maximal
High; die zusätzliche Stufe Critical ist ausschließlich für andere Quellen gedacht.
Siehe [Microsoft Graph Incident](https://learn.microsoft.com/en-us/graph/api/resources/security-incident).

Jeder Batch-Eintrag wird separat geprüft und nur das passende Incident-Objekt
weitergeleitet. Ein High-Incident nimmt keine Low-/Informational-Geschwister mit.
Das ausgewählte Severity-Feld darf auch z. B. `$.severity` oder `$.priority` heißen,
muss aber Textwerte `informational`, `low`, `medium`, `high` oder `critical` liefern.
Groß-/Kleinschreibung und umgebende Leerzeichen sind egal; `info` wird als
`informational` erkannt. Unbekannte, numerische oder fehlende Werte alarmieren nicht.
Eine numerische Priorität `1`/`2` wird nicht automatisch interpretiert.

Sind Score- und Schweregrad-Filter beide aktiv, müssen beide Felder auf demselben
Alarmobjekt liegen und beide Bedingungen erfüllen. Ein hoher Score auf einem
anderen Batch-Eintrag reicht nicht. Die Testseite meldet `severity_missing`,
`severity_invalid`, `severity_below_threshold` oder `filters_no_joint_match` und
zeigt für passende Alarme die tatsächlich gefilterte Payload. Aktive Filter ergänzen
das generische Ereignis um `severity` (normalisiert) und `minimum_severity`;
PagerDuty übernimmt den ausgewählten Schweregrad auch bei einem eigenen Feldnamen.

Bestehende Connectoren und alte Backups behalten zunächst alle Schweregrade.
Den Filter bewusst pro Connector aktivieren. Änderungen an Pfad oder Mindestwert
brechen wartende Zustellungen ab und lassen offene Testformulare ablaufen;
die Änderung selbst holt keine früheren Alarme nach. Beim Wechsel vom vollständigen
Batch auf gefilterte Einzelobjekte ändert sich jedoch die Deduplizierungsgrundlage:
Ein später erneut eingehender, zuvor als Teil des Batches gesendeter Incident kann
einmalig erneut zugestellt werden. Ein bereits laufender HTTP-Aufruf kann noch
abschließen. Beim Rollback werden Connectoren mit aktivem
Severity-Filter deaktiviert, weil die alte Version deren Filter nicht durchsetzen kann.

## Diagnose und Testalarm

**Alert testen** steht in Übersicht, Bearbeitung und Zustellverlauf zur Verfügung
(`endpoint:test`). Eine eingefügte JSON-Payload oder die letzte gespeicherte Meldung
des Quell-Endpoints (`history:read`) wird zunächst ohne Versand geprüft.
Die Diagnose erklärt Kundenzuordnung, fehlende/ungültige Scores, Schwellwert,
Wartung, Pausierung, Entwurf, Recovery, Routing-Verwerfung und gesperrtes Forwarding.
Ein bereits deduplizierter Alarm wird als solcher angezeigt.

Die Vorschau zeigt die bereinigte ausgehende Payload und die gespeicherten
Ziel-Hosts. Identitätsheader aus CIPP (`TaskInfo.Parameters.Headers`) und
`PostExecutionResults` mit internen URLs werden nicht weitergegeben.
**Testalarm senden** erfordert eine ausdrückliche Bestätigung im Formular.
Er erzeugt Zustellungen in derselben dauerhaften Warteschlange wie echte Alarme,
mit `test: true` und eigener stabiler Zustell-ID. Worker und Beat übernehmen den
Versand; Status, HTTP-Code und Fehler sind auf der Testseite sichtbar.
Der Test kann die echte Bereitschafts-Kette auslösen. Er erzeugt keine
ConnectWise-Tickets und verändert keine eingehenden Alarme. Wiederholtes Absenden
desselben Formulars erzeugt keine doppelten Zustellungen. Nach einer Änderung der
Ziele oder des Formats muss die Testseite neu geladen werden.

## PagerDuty Events v2

Als **Webhook-Format** explizit **PagerDuty Events v2** auswählen. Ziel ist je nach
PagerDuty-Region `https://events.pagerduty.com/v2/enqueue` oder
`https://events.eu.pagerduty.com/v2/enqueue`. Andere Hosts/Pfade werden für dieses
Format abgelehnt, damit der Integration Key nur PagerDuty erreicht.
Den 32-stelligen Integration Key einer **Events API v2 Integration** hinterlegen;
ein REST-API-Token oder generischer Bearer-Token erfüllt diese Aufgabe nicht.
Der Key wird verschlüsselt gespeichert, erst beim Versand als `routing_key` in
den JSON-Body eingesetzt und in Vorschau, Warteschlange und Zustellverlauf maskiert
bzw. weggelassen. Bearer- und HMAC-Header werden im PagerDuty-Modus nicht gesendet.

Hookwise erzeugt `event_action: trigger`, einen über alle Wiederholungen stabilen
`dedup_key` und `payload` mit `summary`, `source`, `severity` und `custom_details`.
Die CIPP-Einstufung wird für PagerDuty übersetzt: `high → critical`,
`medium → error`, `low → warning`, `informational → info`. Das aktiviert keinen
numerischen Score-Filter. Ohne Severity-Filter werden Batches als ein Ereignis mit vollständigen
bereinigten Details übergeben; die höchste enthaltene Severity wird verwendet.
PagerDuty entscheidet anhand von Service, Bereitschaft und Eskalationsregeln
über Anruf, Push oder SMS. Ein HTTP-202 bestätigt die Annahme des Ereignisses,
keinen tatsächlich ausgeführten Anruf.
Siehe [PagerDuty Events API v2](https://docs.pagerduty.com/developer/send-alert-event).

## Endpoint: 5. Additional Delivery

Im fünften Schritt der Endpoint-Konfiguration sind beide Zustellwege standardmäßig aktiv:

- **Deliver to ConnectWise** deaktivieren, um ausschließlich externe Webhooks zu verwenden.
  Auch bereits eingereihte Ticketaufgaben, Replays, Recovery und Stale-Tickets führen
  dann keine ConnectWise-Zustellung aus. Bestehende Tickets bleiben offen.
- **Allow Forwarding connectors to listen** steuert, ob Forwarding diesen Endpoint
  als Quelle verwenden darf. Deaktivieren verhindert neue Zustellungen und bricht
  ausstehende Zustellungen bei der nächsten Prüfung durch den Dispatcher ab.
- Die Liste zeigt Anzahl, Namen, Kunden, Score-Bedingungen und Aktiv-/Pause-Status
  der zugeordneten Connectoren. Ohne Connector findet keine externe Zustellung statt.

Beide Einstellungen bleiben beim Klonen, Exportieren und Wiederherstellen erhalten.

## Kundenzuordnung und Verhalten

- Eine Konfiguration gehört immer zu genau einem Kunden und Quell-Endpoint.
  Mehrere Ziele pro Kunde sind möglich.
- Die Kundenauflösung berücksichtigt JSON-Mapping, Routing-Regeln, den
  `#CW`-Marker, TenantMap und die konfigurierte Standardfirma. Auf gemeinsam
  genutzten Endpoints mit globalem Routing werden unbekannte oder mehrdeutige
  Tenants ohne explizite Kundenauflösung nicht an eine Standardfirma weitergeleitet.
- Der Tenant steht wie bei der bestehenden Verarbeitung auf Payload-Ebene
  oder in `TaskInfo`; mandantenübergreifende Batches mit wechselndem Tenant
  innerhalb der einzelnen Ergebnisse werden nicht unterstützt.
- Pausierte, archivierte oder als Entwurf gespeicherte Quellen,
  Wartungsfenster, verworfene Meldungen und Recovery-Meldungen lösen nichts aus.
- Die Zustellung wird mit der eingehenden Meldung in derselben
  Datenbanktransaktion vorgemerkt. ConnectWise-Ticketfehler blockieren sie nicht.
- Der Celery-Beat-Job `hookwise.dispatch_customer_webhooks` prüft alle zehn
  Sekunden auf fällige Zustellungen. Web, Worker und Beat müssen denselben
  aktuellen Code und dieselbe Datenbank verwenden.
- Identische Alarmobjekte werden je Webhook anhand ihres Inhalts dedupliziert,
  solange ihr Zustelleintrag existiert. Geänderte Alarmdaten können erneut auslösen.
  Beim Pausieren werden bereits vorgemerkte Zustellungen beim nächsten Lauf
  abgebrochen; beim Aktivieren werden diese nicht nachgeholt.

## Empfänger-Vertrag

Hookwise sendet `POST` mit `Content-Type: application/json`:

```json
{
  "event": "hookwise.customer_alert",
  "version": 1,
  "company_id": "123",
  "source_endpoint_id": "source-id",
  "source": {"id": "source-id", "name": "Cipp"},
  "request_id": "incoming-request-id",
  "score": 85.0,
  "minimum_score": 70.0,
  "alert": {"AlertId": "defender-alert-1", "Score": 85}
}
```

`alert` enthält nur das Objekt, in dem sich das passende Score-Feld befindet.
`source_endpoint_id` und `source.id` sind stabile SHA-256-Referenzen; sie enthalten
nicht den privaten Ingress-Token des Quell-Endpoints. `source.name` ist dessen Anzeigename.
Andere Batch-Einträge und eingehende HTTP-Header werden nicht mitgeschickt.
Bekannte Geheimnisfelder werden durch Hookwise maskiert. Mit `$.score` auf
oberster Ebene ist das Alarmobjekt die gesamte eingehende JSON-Payload.

`Idempotency-Key` und `X-HookWise-Delivery-ID` enthalten dieselbe stabile
Zustell-ID. Der Empfänger sollte diese dauerhaft deduplizieren, bevor er eine
Alarmierungskette startet: Wenn die Antwort verloren geht, kann derselbe
Alarm mehrfach eintreffen.

Optional sendet Hookwise `Authorization: Bearer <Token>`. Ist ein
Signaturschlüssel hinterlegt, kommen diese Header dazu:

- `X-HookWise-Timestamp`: Unix-Zeit in Sekunden, pro Versuch neu erzeugt.
- `X-HookWise-Signature`: `sha256=<hex HMAC-SHA256>` über
  `timestamp + "." + unveränderte UTF-8-Bytes des HTTP-Bodys`.

Der Empfänger prüft die Signatur mit konstantem Zeitvergleich und akzeptiert
nur aktuelle Zeitstempel, z. B. innerhalb von fünf Minuten.

Jede HTTP-Antwort von 200 bis 299 gilt als erfolgreich. Netzwerkfehler und
HTTP 408, 425, 429 sowie 5xx werden bis zu insgesamt fünf Versuchen wiederholt;
die Wartezeiten sind 60, 120, 240 und 480 Sekunden zuzüglich Beat-Intervall.
Andere Antworten, einschließlich Redirects, beenden die Zustellung als Fehler.
Timeouts: fünf Sekunden Verbindungsaufbau, zehn Sekunden Lesen der Antwortheader.
Im Menü **Zustellungen** stehen Status, HTTP-Code und Versuche.

URL, Bearer-Token, Signaturschlüssel und PagerDuty Integration Key werden verschlüsselt gespeichert.
Die URL wird beim Bearbeiten erst durch **Ziel bearbeiten** geladen
(`endpoint:write` plus `secret:reveal`, CSRF-geschützter POST, Audit-Eintrag,
`Cache-Control: no-store`). Ohne Reveal-Berechtigung kann ein Ziel vollständig
ersetzt werden. Zugangsdaten werden nie vorbefüllt. Lokale/private Zieladressen und Redirects
werden nicht zugelassen. Die geprüfte öffentliche DNS-Adresse wird für die
TLS-Verbindung festgesetzt; Zertifikat und SNI werden gegen den ursprünglichen
Hostnamen geprüft ([urllib3-Dokumentation](https://urllib3.readthedocs.io/en/stable/advanced-usage.html#custom-sni-hostname)).
Lesen verwendet `endpoint:read`, Änderungen `endpoint:write`, der
Zustellverlauf `history:read`. Änderungen erscheinen im Audit Log.
Konfigurationen sind im verschlüsselten Konfigurationsbackup enthalten;
Zustellverlauf und Warteschlange erfordern ein vollständiges Datenbankbackup.

Ändert sich die Ziel-URL, werden die bisherigen Zugangsdaten entfernt und
ausstehende Zustellungen für das alte Ziel abgebrochen. Neue Zugangsdaten müssen
für das neue Ziel eingetragen werden. Das gilt auch beim Wiederherstellen eines
Backups; unveränderte alte Zugangsdaten werden dabei nicht übernommen. Ein bereits
laufender HTTP-Aufruf kann noch am ursprünglichen Ziel ankommen.
Änderungen des Webhook-Formats oder PagerDuty Integration Keys brechen ebenfalls
ausstehende Zustellungen ab, damit alte Alarme nicht mit neuem Vertrag versendet werden.

Abgeschlossene, fehlgeschlagene und abgebrochene Zustellungen entfernen ihren
gespeicherten JSON-Inhalt sofort. Ihre Metadaten werden nach der konfigurierten
Log-Aufbewahrungsfrist (`LOG_RETENTION_DAYS` bzw. Settings) gelöscht. Wartende und
laufende Zustellungen behalten ihren Inhalt für die Zustellversuche. **Clear completed
history** erfordert `history:delete` und entfernt nur abgeschlossene Einträge.
Explizites Löschen der zugehörigen eingehenden Logs entfernt auch alle Kopien in
Forwarding, einschließlich wartender Zustellungen. Nach dem Löschen eines
Zustelleintrags kann derselbe Alarminhalt erneut zugestellt werden.

HTTPS/TLS verschlüsselt die Verbindung. Die optionale HMAC-Signatur prüft
Herkunft und Integrität; sie verschlüsselt den JSON-Inhalt nicht zusätzlich.
Eine zusätzliche Payload-Verschlüsselung, mTLS-Clientzertifikate und OAuth2
sind derzeit nicht implementiert. Mit dem Empfänger sind das JSON-Format,
Pflichtfelder, Authentifizierung, Signaturprüfung und Deduplizierung über die
Zustell-ID abzustimmen.

## Deployment

Hookwise führt erforderliche Alembic-Upgrades beim Start von Web, Worker und Beat
automatisch aus. Gleichzeitige Starts werden über eine Datenbanksperre serialisiert.
Ein fehlgeschlagenes Upgrade verhindert den Start. Compose behält zusätzlich seinen
vorgeschalteten Migrationsdienst. Manuelle Migrationen bleiben möglich:

```bash
flask --app hookwise:create_app db upgrade
```

`AUTO_DB_UPGRADE=false` deaktiviert die automatische Migration für Installationen
mit separat verwalteten Upgrades; explizite `flask db`-Befehle ändern immer nur die
angeforderte Revision. Die Migrationen legen die Forwarding-Tabellen und
Zustellkontrollen an; bestehende Endpoints behalten beide Zustellwege aktiviert.
Eine echte Zustellung lässt sich mit einer zum Score-Pfad passenden Meldung
an den konfigurierten Quell-Endpoint prüfen; sie kann die Kundenalarmierung starten.
