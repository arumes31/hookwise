# CIPP Defender Incidents: High und noch nicht erledigt

Hookwise kann den Schweregrad und den Status desselben Incident-Objekts
gemeinsam prüfen. Die Konfiguration für die bisherige CIPP-Payload lautet:

| Einstellung | Wert |
| --- | --- |
| Schweregrad-Pfad | `$.Results[*].Severity` |
| Mindest-Schweregrad | `high` |
| Status-Pfad | `$.Results[*].Status` |
| Zulässige offene Status | `active`, `inProgress`, `awaitingAction` |
| Numerischer Score-Filter | Aus, solange kein numerisches Score-Feld geliefert wird |

`resolved` und `redirected` lösen keine neue Alarmierung aus. Ein fehlender,
leerer oder unbekannter Status gilt nicht als offen. Hookwise fragt dafür
Microsoft Graph nicht selbst ab und nimmt einen fehlenden Status auch nicht
aufgrund des CIPP-Kommandonamens als `active` an.

## Was CIPP heute tatsächlich liefert

Geprüft am 24.10.2026 anhand von CIPP-API **11.2.1**, Commit
[`8985c1f3294f1dd5cb16a85c82753a40c9029224`](https://github.com/KelvinTegelaar/CIPP-API/commit/8985c1f3294f1dd5cb16a85c82753a40c9029224).
Das ist eine Prüfung des öffentlichen Upstream-Codes, keine Bestätigung der
installierten Version oder lokaler Anpassungen der eigenen CIPP-Instanz.

[`Get-CIPPAlertDefenderIncidents.ps1`](https://github.com/KelvinTegelaar/CIPP-API/blob/8985c1f3294f1dd5cb16a85c82753a40c9029224/Modules/CIPPAlerts/Public/Alerts/Get-CIPPAlertDefenderIncidents.ps1)
fragt bereits Folgendes ab:

```powershell
https://graph.microsoft.com/v1.0/security/incidents?`$top=50&`$filter=status eq 'active'
```

Die Standardabfrage liefert also bereits nur Incidents mit Graph-Status
`active`. Erledigte Incidents aus der Defender-Konsole wären damit zum Zeitpunkt
dieser Abfrage nicht enthalten. Allerdings entfernt CIPP `status` anschließend
durch die feste Auswahl der Ausgabefelder; `IncidentID` und `Severity` werden
übernommen, `Status` bisher nicht. Die von uns untersuchte Payload entspricht
dieser Form.

Die Funktion bietet als Eingabe einen Severity-Filter (`IncidentSeverities`),
aber keinen Schalter zum Hinzufügen von Ausgabefeldern oder zur Auswahl weiterer
Incident-Status. Eine Hookwise-Einstellung allein ergänzt das fehlende Feld nicht.
Auch `TaskInfo.TaskState` bezeichnet den Scheduler-Task und ist kein Ersatz für
den Defender-Incident-Status.

[`Send-CIPPScheduledTaskAlert.ps1`](https://github.com/KelvinTegelaar/CIPP-API/blob/8985c1f3294f1dd5cb16a85c82753a40c9029224/Modules/CIPPCore/Public/Send-CIPPScheduledTaskAlert.ps1)
übernimmt die zurückgegebenen Objekte in `Results`. Bei aktiviertem CIPP-Modus
`UseStandardizedSchema` heißt die äußere Eigenschaft stattdessen `results`;
dann müssen beide Hookwise-Pfade entsprechend `$.results[*].Severity` und
`$.results[*].Status` lauten. Den Schema-Modus nicht nebenbei umstellen:
auch andere bestehende Zuordnungen können vom bisherigen Format abhängen.

## Minimale Änderung auf der CIPP-Seite

Der beiliegende [Patch](examples/cipp-defender-incident-status.patch) fügt der
vorhandenen Projektion genau ein Feld hinzu:

```powershell
Status         = $Incident.status
```

Er lässt die bisherige Graph-Abfrage, Mandantenzuordnung und Severity-Auswahl
unverändert. Es werden keine zusätzlichen Graph-Berechtigungen oder separaten
Zugangsdaten benötigt, da die bestehende Incident-Antwort dieses Feld bereits
enthält. Der Patch ist zur Prüfung und Übernahme in den eigenen CIPP-API-Fork
gedacht; Hookwise führt ihn nicht aus und ändert keine CIPP-Installation.

Im eigenen CIPP-API-Checkout vor der Übernahme ausführen:

```powershell
git apply --check <Pfad-zur-Hookwise-Kopie>/docs/examples/cipp-defender-incident-status.patch
git apply <Pfad-zur-Hookwise-Kopie>/docs/examples/cipp-defender-incident-status.patch
```

Danach die Änderung über den für diese CIPP-Instanz eingerichteten
Deployment-Prozess bereitstellen. Lokale Anpassungen im Fork bei CIPP-Updates
erneut abgleichen. Diese Hookwise-Änderung veröffentlicht keinen CIPP-Upstream-PR
und stellt die CIPP-Anpassung nicht bereit.

Ein minimales eingehendes Objekt sieht danach beispielsweise so aus:

```json
{
  "Tenant": "customer.example",
  "TaskInfo": { "Command": "Get-CIPPAlertDefenderIncidents" },
  "Results": [
    {
      "IncidentID": "example-incident",
      "IncidentName": "Example Defender incident",
      "Severity": "high",
      "Status": "active",
      "Tenant": "customer.example"
    }
  ]
}
```

Die vereinfachte Payload ist für die Filterprüfung gedacht. Die tatsächliche
Kundenzuordnung benötigt die zum Quell-Endpoint passende TenantMap bzw.
Routing-Konfiguration.

## CIPP-Deduplizierung und Snoozes beim Umstieg

Die zusätzliche Eigenschaft verändert in der geprüften Version die Identität
des CIPP-Alert-Items:

- [`Get-AlertContentHash`](https://github.com/KelvinTegelaar/CIPP-API/blob/8985c1f3294f1dd5cb16a85c82753a40c9029224/Modules/CIPPCore/Public/GraphHelper/Get-AlertContentHash.ps1)
  verwendet bevorzugt `UserPrincipalName`, `Id` oder `Message`. Die vorhandene
  Defender-Projektion hat nur `IncidentID`; deshalb greift hier der Hash über
  das vollständige, sortierte JSON. Auch Status- oder Severity-Änderungen können
  damit ein neues Item ergeben.
- [`Write-AlertTrace`](https://github.com/KelvinTegelaar/CIPP-API/blob/8985c1f3294f1dd5cb16a85c82753a40c9029224/Modules/CIPPCore/Public/GraphHelper/Write-AlertTrace.ps1)
  vergleicht diese Hashes mit `AlertLifecycle`: neue bzw. wieder geöffnete,
  nicht gesnoozte Items werden ausgegeben; weiterhin vorhandene normalerweise
  nicht. Bei aktiviertem `AlertReNotify` können auch weiterhin vorhandene Items
  erneut ausgegeben werden.
- Bestehende Incidents können durch das neue Feld einmalig als neu erscheinen.
  Bereits gesetzte Snoozes gehören zum alten Hash und greifen für das geänderte
  Objekt möglicherweise nicht mehr. Es gibt in diesem Patch keine Migration
  dieser Zustände. Alle Post-Execution-Kanäle des Tasks können betroffen sein,
  nicht nur Hookwise.

Für einen kontrollierten Umstieg:

1. Aktuelle offene High-Incidents und vorhandene Snoozes vorab prüfen. Festlegen,
   welche bestehenden Vorfälle bewusst nachalarmiert werden sollen.
2. Den betroffenen CIPP-Task während der Änderung pausieren. In Hookwise den
   Connector vorübergehend deaktivieren und bestehende ausstehende Zustellungen
   prüfen. Eine bereits laufende HTTP-Zustellung kann noch abschließen.
3. Für einen bewussten Basislauf die Post-Execution-Kanäle des betroffenen
   CIPP-Tasks vorübergehend deaktivieren; auch E-Mail/PSA/Push berücksichtigen.
   Die geänderte Projektion bereitstellen und einen Lauf ohne Benachrichtigungen
   durchführen. Dieser Lauf aktualisiert den CIPP-Lifecycle und liefert
   bestehende Incidents später normalerweise nicht erneut aus. Er darf deshalb
   nicht stillschweigend als Nachalarmierung eingeplant werden.
4. Benötigte Snoozes anhand der geänderten Items erneut setzen. Die vorhandenen
   Daten nicht pauschal löschen und zur scheinbaren Stabilisierung nicht einfach
   `Id` ergänzen: Auch das würde die Hash-Grundlage ändern und wäre eine eigene
   Migration.
5. In Hookwise High und den Status-Filter aktivieren, zunächst mit einer Payload
   ohne Versand prüfen: High/active zugelassen, High/resolved und High/redirected
   blockiert, High ohne Status blockiert, Medium/active blockiert. In einem Batch
   dürfen High/resolved und Low/active zusammen ebenfalls nicht passieren.
6. Die tatsächliche CIPP-Ausgabe auf `Results[*].Status` prüfen. Erst dann die
   vorgesehenen CIPP-Kanäle, den Hookwise-Connector und den normalen Zeitplan wieder
   aktivieren. Falls für eine echte Ende-zu-Ende-Prüfung ein Testalarm versandt
   wird, die Bereitschaftskette vorher berücksichtigen.

Ein leerer neuer Webhook-Verlauf beweist keinen Fehler: CIPP kann einen bereits
bekannten Incident vorher unterdrückt haben. Umgekehrt bewirkt das Aktivieren
des Hookwise-Filters weder einen CIPP-Neulauf noch ein Nachholen alter Incidents.

## Optional: weitere noch offene Graph-Status erfassen

Hookwise erlaubt auch `inProgress` und `awaitingAction`, weil diese Incidents noch
nicht erledigt sind. Die unveränderte CIPP-Standardabfrage liefert diese Status
jedoch nicht. Soll der CIPP-Task bewusst alle drei offenen Status beobachten,
ist zusätzlich eine gesonderte Änderung seiner Abfrage nötig, beispielsweise:

```powershell
$Incidents = New-GraphGetRequest -uri "https://graph.microsoft.com/v1.0/security/incidents?`$top=50&`$filter=status eq 'active' or status eq 'inProgress' or status eq 'awaitingAction'" -tenantid $TenantFilter -extraHeaders @{ Prefer = 'include-unknown-enum-members' }
```

Das ist **nicht** Bestandteil des minimalen Patches: Es erweitert den Umfang
des Alerts und kann weitere bestehende Vorfälle erstmals melden. Für diese
Änderung gilt derselbe kontrollierte Basislauf. `awaitingAction` ist ein
erweiterbarer Graph-Enum-Wert; Microsoft verlangt für dessen Rückgabe den
`Prefer: include-unknown-enum-members`-Header. Die geprüfte CIPP-Version unterstützt
diesen Header über `New-GraphGetRequest -extraHeaders`.

Die Statusbedeutungen sind in der
[Microsoft-Graph-Incident-Dokumentation](https://learn.microsoft.com/en-us/graph/api/resources/security-incident?view=graph-rest-1.0)
beschrieben. `redirected` bedeutet in einen anderen Incident zusammengeführt;
stattdessen muss der Ziel-Incident gegebenenfalls separat geprüft werden.
Ein Status im Webhook ist ein Stand zum Abfragezeitpunkt. Die neue Bedingung
verhindert keine Statusänderung in Defender zwischen Abfrage und späterem
Versand und löst einen bereits ausgelösten PagerDuty-Vorfall nicht automatisch auf.
