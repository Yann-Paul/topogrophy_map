# Topografie-Kartenposter

Das Projekt erstellt druckfähige Karten als PNG oder PDF. Die interaktive Oberfläche liegt in `index.html`; `poster.py` erzeugt Exporte über die Kommandozeile und stellt außerdem den lokalen Server für den Python-Export der Webseite bereit.

## Voraussetzungen und Start

Für den Python-Export werden Python 3 und folgende Pakete benötigt:

```powershell
py -m pip install pyproj pillow reportlab requests numpy
```

Im Projektordner die Oberfläche samt lokalem Exportserver starten:

```powershell
py poster.py --serve
```

Anschließend im Browser **http://localhost:8000** öffnen. Das Terminal muss während des Python-Exports geöffnet bleiben. Mit `Strg+C` wird der Server beendet. Falls Port 8000 belegt ist, lässt sich ein anderer Port angeben, zum Beispiel `py poster.py --serve --port 8765`; die Seite ist dann unter `http://localhost:8765` erreichbar.

Die Webseite kann auch direkt als Datei geöffnet werden. In diesem Modus funktioniert der browserinterne Export; **Mit Python exportieren** benötigt dagegen den gestarteten Server und die über dessen Adresse geladene Seite.

## Programmablauf

1. Die Seite zeigt eine verschiebbare und zoombare Kartenansicht. Grundkarte, Projektion und Relief-Einstellungen bestimmen die Vorschau.
2. Eine Region wird aus der aktuellen Ansicht übernommen, per Maus als Rechteck aufgezogen oder durch Koordinaten festgelegt. Die Koordinatenfelder sind West, Süd, Ost und Nord in Grad.
3. Papiergröße, Ausgabeformat, Auflösung und Beschriftung werden eingestellt. Die Seitenhöhe wird anhand der Region und des Seitenverhältnisses automatisch berechnet, solange sie nicht manuell geändert wird.
4. **Exportieren** rendert die Ausgabe im Browser blattweise. **Mit Python exportieren** übergibt dieselben Karteneinstellungen an `poster.py`; der Server verfolgt den Auftrag und stellt die fertige Datei zum Download bereit.
5. Der Python-Export lädt benötigte Karten- und Höhendaten bei Bedarf herunter und speichert sie im Cache. Bereits vorhandene Kacheln werden bei späteren Läufen wiederverwendet.

## Einstellungen in `index.html`

- **Projektion:** Globus, Mercator, Equal Earth, Robinson oder Lambert-Azimutal. Die Varianten „Weltkarte“ zeigen Equal Earth beziehungsweise Robinson als Weltkarte.
- **Karte:** Satellitenbild, topografische Karte, OpenStreetMap oder weißer Hintergrund.
- **Relief:** Z-Faktor, Sonnenrichtung (Azimut), Sonnenhöhe, Stärke und Glanzlichter steuern die Schummerung aus Höhendaten. Stärke 0 schaltet sie aus.
- **Beschriftung:** Namen können ausgeschaltet oder weiß/schwarz dargestellt werden. Label-Zoom steuert die Dichte der Namen; ein leeres Feld folgt dem aktuellen Ansichtszoom. Schriftgröße wird in Punkt angegeben. Die Option **Punkte** ergänzt beim Python-Export Punkte neben Städten und Bergen.
- **Region:** Werte direkt eingeben, **Region aus Ansicht übernehmen** wählen oder **Rechteck in Karte zeichnen**. Das fertige Rechteck lässt sich an seiner Umrandung verschieben.
- **Ausgabe:** Breite und DPI bestimmen die Pixelauflösung. A4 bis A0 und B0 teilen große Ausgaben auf druckbare Blätter auf. **Volles Bild (ein Blatt)** erzeugt ein einzelnes Blatt in der gesamten Bildgröße.
- **Format:** PNG und JPG werden blattweise gespeichert; PDF enthält eine Seite je Blatt. Der Python-Export unterstützt PNG und PDF, nicht JPG.
- **Rechen-Kachel cm:** Nur für Python-PDFs. Begrenzt die Größe der internen Berechnungsbereiche; die fertige PDF bleibt ein großes Blatt. 0 deaktiviert diese Aufteilung.

## Exportwege

### Browser-Export

**Exportieren** rendert die Karte mit dem Browser und startet den Download. PNG und JPG ergeben je Druckblatt eine Datei; PDF ergibt eine Datei mit einer Seite pro Blatt. Bei Formaten A4–B0 kann eine große Karte daher mehrere Blätter umfassen. Für eine Ausgabe ohne Aufteilung **Volles Bild (ein Blatt)** auswählen.

Der Browser-Export ist durch Browser-Canvas-Grenzen beschränkt (maximal 16.384 Pixel je Canvas-Seite). Bei einer entsprechenden Fehlermeldung DPI oder Breite reduzieren oder ein Papierformat auswählen. Die Vektorkartenbeschriftung der Vorschau wird beim Browser-Export in das Bild gerendert.

### Python-Export über die Webseite

**Mit Python exportieren** verwendet dieselbe Region und die Einstellungen der Seite, berechnet die Ausgabe aber mit Python. Die Datei wird zunächst im Ordner `poster_out/` abgelegt und über den Link in der Statuszeile heruntergeladen. Der Python-Export umgeht die Blatt- und Canvas-Aufteilung des Browser-Exports; PDF-Beschriftungen und Linien bleiben Vektoren, während das Kartenbild ein Raster ist. PNG ist ein einzelnes Rasterbild.

Falls der Server nicht erreichbar ist: `py poster.py --serve` im Projektordner starten und die Seite über die angezeigte lokale Adresse neu öffnen. Globus-Projektion und JPG werden vom Python-Export nicht unterstützt.

## Kommandozeilen-Export

Ein kleiner Testexport:

```powershell
py poster.py --width-mm 100 --dpi 150 --zoom 6 --out test.pdf
```

Beispiel für eine Region mit Robinson-Projektion und Relief:

```powershell
py poster.py --region -25 34 45 72 --proj rob --hs-str 0.33 --out region.pdf
```

Ohne `--region` verwendet das Programm den voreingestellten Ausschnitt in EPSG:3035. Die wichtigsten Optionen:

| Option | Bedeutung |
| --- | --- |
| `--out` | Ausgabepfad; Standard: `europa.pdf` |
| `--base` | `sat`, `topo`, `osm` oder `none` |
| `--extent XMIN YMIN XMAX YMAX` | Ausschnitt in EPSG:3035-Metern, wenn `--region` fehlt |
| `--region W S O N` | Ausschnitt in geografischen Grad; ersetzt `--extent` |
| `--proj` | Projektion, z. B. `mercator`, `ee`, `rob` oder `laea` |
| `--width-mm`, `--dpi` | Ausgabegröße und Auflösung |
| `--zoom` | Kartenkachel-Zoom; standardmäßig wird nicht hochskaliert |
| `--pt`, `--label-zoom`, `--text` | Beschriftungsgröße, Beschriftungsdichte und Schriftfarbe |
| `--no-labels`, `--dots` | Beschriftungen/Grenzen ausblenden oder Punkte ergänzen |
| `--hs-str`, `--hs-zf`, `--hs-az`, `--hs-alt`, `--hs-gl` | Reliefstärke, Z-Faktor, Sonnenazimut, Sonnenhöhe und Glanzlichter |
| `--dem-zoom` | Zoomstufe der Höhendaten (maximal 15) |
| `--tile-mm` | Interne Berechnungs-Kachelgröße in Millimetern; 0 = ein Block |
| `--cache` | Cache-Ordner; Standard: `tile_cache` |

## Daten und Dateien

Kartengrundlagen kommen von Esri (Satellit und Topografie) beziehungsweise OpenStreetMap. Reliefdaten stammen aus Terrarium-Höhenkacheln; Länder-, Grenzen-, Städte-, Fluss- und Bergdaten werden aus Natural Earth GeoJSON geladen. Für diese Datenquellen ist beim ersten Abruf eine Internetverbindung nötig.

- `tile_cache/`: zwischengespeicherte Karten- und Höhendaten sowie Natural-Earth-Dateien. Fehlende Kacheln können bei einem späteren Lauf erneut geladen werden.
- `poster_out/`: über den lokalen Server erstellte Python-Exporte.
- `index.html`: interaktive Kartenoberfläche und Browser-Export.
- `poster.py`: Python-Renderer, Kommandozeilenwerkzeug und lokaler Exportserver.

Sehr große Ausgaben benötigen entsprechend viel Rechenzeit und Speicherplatz. Die Kachelverarbeitung begrenzt den Speicherbedarf, und ein abgebrochener Lauf kann den vorhandenen Cache weiterverwenden.
