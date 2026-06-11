import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import seaborn as sns

# Design für die Grafiken festlegen
sns.set_theme(style="whitegrid")
plt.rcParams['figure.figsize'] = [12, 6]
plt.rcParams['font.size'] = 11


# ==========================================
# 1. DATEN-AUFBEREITUNG (BEIDE DATEIEN)
# ==========================================

def load_combined_data(redispatch_path, generation_path):
    # --- 1a. Redispatch-Daten einlesen & stündlich aggregieren ---
    df_red = pd.read_csv(redispatch_path, sep=";", low_memory=False)
    df_red["Zeitstempel"] = pd.to_datetime(
        df_red["BEGINN_DATUM"] + " " + df_red["BEGINN_UHRZEIT"], format="%d.%m.%Y %H:%M"
    )
    df_red_filtered = df_red[
        (df_red["ANWEISENDER_UENB"] == "TenneT DE")
        & (df_red["PRIMAERENERGIEART"] == "Erneuerbar")
        & (df_red["RICHTUNG"] == "Wirkleistungseinspeisung reduzieren")
        ].copy()

    if df_red_filtered["GESAMTE_ARBEIT_MWH"].dtype == object:
        df_red_filtered["GESAMTE_ARBEIT_MWH"] = df_red_filtered["GESAMTE_ARBEIT_MWH"].str.replace(",", ".").astype(
            float)

    df_red_filtered["Stunde"] = df_red_filtered["Zeitstempel"].dt.floor("h")
    hourly_red = df_red_filtered.groupby("Stunde")["GESAMTE_ARBEIT_MWH"].sum().to_frame("Abregelung_MWh")

    # --- 1b. Erzeugungs-Daten einlesen & von 15-Minuten auf Stunden aggregieren ---
    df_gen = pd.read_csv(generation_path, sep=";", low_memory=False)
    df_gen["Zeitstempel"] = pd.to_datetime(df_gen["Datum von"], format="%d.%m.%Y %H:%M")

    pv_col = "Photovoltaik [MWh] Originalauflösungen"

    # SAUBERE BEREINIGUNG:
    # 1. Bindestriche "-" (Nachtwerte) durch 0 ersetzen
    df_gen[pv_col] = df_gen[pv_col].replace("-", "0")

    # 2. Wenn es Text ist, Tausendertrennpunkte entfernen und Komma zu Punkt wandeln
    if df_gen[pv_col].dtype == object:
        df_gen[pv_col] = (
            df_gen[pv_col]
            .str.replace(".", "", regex=False)  # Punkte löschen (z.B. 1.234 -> 1234)
            .str.replace(",", ".", regex=False)  # Komma zu Punkt (z.B. 1234,50 -> 1234.50)
        )

    # 3. In Fließkommazahl konvertieren
    df_gen[pv_col] = df_gen[pv_col].astype(float)

    df_gen["Stunde"] = df_gen["Zeitstempel"].dt.floor("h")
    # Viertelstundenwerte für die Stunde aufsummieren
    hourly_pv = df_gen.groupby("Stunde")[pv_col].sum().to_frame("PV_Erzeugung_DE_MWh")

    # --- 1c. Beide Datenströme auf einer 8760h-Timeline zusammenführen ---
    full_year = pd.date_range(start="2025-01-01 00:00:00", end="2025-12-31 23:00:00", freq="h")
    df_timeline = pd.DataFrame(index=full_year)

    df_timeline = df_timeline.join(hourly_red).join(hourly_pv).fillna(0)

    # Relative Solar-Intensität berechnen (0.0 bis 1.0)
    max_pv = df_timeline["PV_Erzeugung_DE_MWh"].max()
    df_timeline["Solar_Intensitaet"] = df_timeline["PV_Erzeugung_DE_MWh"] / max_pv if max_pv > 0 else 0

    return df_timeline


# ==========================================
# 2. ERWEITERTE SIMULATIONS-LOGIK
# ==========================================

def run_advanced_simulation(df, num_evs, acceptance_rate, min_soc, max_soc):
    active_evs = num_evs * acceptance_rate
    total_fleet_capacity_mwh = (active_evs * 60.0) / 1000

    current_soc = 0.4  # Startwert
    saved_energy = []
    soc_history = []

    for idx, row in df.iterrows():
        # Verfügbarkeit an der Wallbox
        avail_factor = 0.40 if 8 <= idx.hour <= 16 else 0.75

        # 1. Normales solares Laden
        normal_solar_charging_mwh = (active_evs * 2.0 * avail_factor * row["Solar_Intensitaet"]) / 1000
        current_soc += normal_solar_charging_mwh / total_fleet_capacity_mwh
        current_soc = min(max_soc, current_soc)

        # 2. Redispatch-Aufnahme
        max_charging_power_mwh = (active_evs * 11.0 * avail_factor) / 1000
        free_space_mwh = total_fleet_capacity_mwh * (max_soc - current_soc)
        max_absorb = min(max_charging_power_mwh, free_space_mwh)

        saved = min(row["Abregelung_MWh"], max_absorb)
        saved_energy.append(saved)

        # 3. ENTLADUNG: Pendeln + ECHTES V2G IN DEN ABENDSTUNDEN
        # Pendelverbrauch zu Stoßzeiten:
        driving_discharge = (active_evs * 1.5) / 1000 if idx.hour in [7, 8, 16, 17] else 0

        # V2G-Rückspeisung: Zwischen 18 und 23 Uhr speisen Autos mit 4 kW ins Netz zurück
        v2g_discharge = 0
        if 18 <= idx.hour <= 23:
            v2g_discharge = (active_evs * 4.0 * avail_factor) / 1000

        total_discharge = driving_discharge + v2g_discharge

        # SoC updaten
        new_energy = ((current_soc * total_fleet_capacity_mwh) + saved - total_discharge)
        current_soc = np.clip(new_energy / total_fleet_capacity_mwh, min_soc, max_soc)
        soc_history.append(current_soc)

    df["Gerettet_MWh"] = saved_energy
    df["SoC_Flotte"] = soc_history
    df["Verbleibende_Abregelung_MWh"] = df["Abregelung_MWh"] - df["Gerettet_MWh"]
    return df


# ==========================================
# 3. ANALYSE UND GRAFIKEN
# ==========================================

# Dateipfade definieren (Passe die Namen an, wenn deine Dateien anders heißen)
redispatch_file = "data/Redispatch_Daten.csv"
generation_file = "data/Realisierte_Erzeugung_202501010000_202601010000_Viertelstunde.csv"

# Daten zusammenführen
df_timeline = load_combined_data(redispatch_file, generation_file)
TENNET_E_AUTOS = 800000

szenarien = [
    ("Szenario 1: Pessimistisch", 0.20, 0.75),
    ("Szenario 2: Realistisch", 0.50, 0.85),
    ("Szenario 3: Optimistisch", 0.85, 0.95)
]

ergebnisse_szenarien = {}
vergleichs_daten = []

print("--- SIMULATIONS-ERGEBNISSE JAHRESBILANZ ---")
for name, acceptance, max_soc in szenarien:
    res = run_advanced_simulation(df_timeline.copy(), TENNET_E_AUTOS, acceptance, min_soc=0.30, max_soc=max_soc)
    ergebnisse_szenarien[name] = res

    total_curt = res["Abregelung_MWh"].sum()
    total_saved = res["Gerettet_MWh"].sum()
    reduction_pct = (total_saved / total_curt) * 100 if total_curt > 0 else 0
    print(f"{name}: {reduction_pct:.2f}% Einsparung ({total_saved:,.1f} MWh von {total_curt:,.1f} MWh)")

    # Hier extra sauber als Dictionary speichern
    vergleichs_daten.append({"Szenario": name, "Reduktion [%]": reduction_pct})

df_vergleich = pd.DataFrame(vergleichs_daten)

# Wir erstellen die Poster-Kürzel direkt über ein sauberes Mapping, das garantiert funktioniert:
szenarien_mapping = {
    "Szenario 1: Pessimistisch": "S. 1 (Pessimistisch)",
    "Szenario 2: Realistisch": "S. 2 (Realistisch)",
    "Szenario 3: Optimistisch": "S. 3 (Optimistisch)"
}
df_vergleich["Szenario_Short"] = df_vergleich["Szenario"].map(szenarien_mapping)

# --- PLOT 1: SZENARIENVERGLEICH JAHRESBILANZ ---
fig_bar, ax_bar = plt.subplots(figsize=(8, 5), dpi=300)
sns.barplot(x="Szenario_Short", y="Reduktion [%]", data=df_vergleich, palette="crest", ax=ax_bar)

ax_bar.set_title("Gesamtreduktion der\nNetzkapazitätsverluste (TenneT 2025)", fontsize=15, fontweight='bold', pad=12)
ax_bar.set_ylabel("Verhinderte Abregelung [%]", fontsize=13, fontweight='bold')
ax_bar.set_xlabel("Simulationsszenarien", fontsize=13, fontweight='bold')
ax_bar.tick_params(axis='both', labelsize=12)
ax_bar.set_ylim(0, 100)

for p in ax_bar.patches:
    height = p.get_height()
    ax_bar.annotate(f"{height:.1f}%",
                    (p.get_x() + p.get_width() / 2., height + 2.5),
                    ha='center', va='bottom', fontsize=13, fontweight='bold', color='black')

plt.tight_layout()
# ÄNDERUNG: Bild speichern statt plt.show()
plt.savefig("plot_szenarienvergleich.png", bbox_inches='tight')
plt.close()

# Monats-Konfigurationen für danach
monate_config = [
    {"name": "Juni", "start": "2025-06-01", "end": "2025-06-30 23:59:00", "woche_start": "2025-06-09",
     "woche_end": "2025-06-16"},
    {"name": "Juli", "start": "2025-07-01", "end": "2025-07-31 23:59:00", "woche_start": "2025-07-07",
     "woche_end": "2025-07-14"},
    {"name": "September", "start": "2025-09-01", "end": "2025-09-30 23:59:00", "woche_start": "2025-09-08",
     "woche_end": "2025-09-15"}
]

sz_styles = {
    "Szenario 1: Pessimistisch": {"color": "orange", "linestyle": ":", "linewidth": 2,
                                  "label_clean": "Szenario 1 (Pessimistisch)"},
    "Szenario 2: Realistisch": {"color": "teal", "linestyle": "--", "linewidth": 2,
                                "label_clean": "Szenario 2 (Realistisch)"},
    "Szenario 3: Optimistisch": {"color": "green", "linestyle": "-", "linewidth": 2,
                                 "label_clean": "Szenario 3 (Optimistisch)"}
}

wochentage_de = {"Mon": "Mo", "Tue": "Di", "Wed": "Mi", "Thu": "Do", "Fri": "Fr", "Sat": "Sa", "Sun": "So"}

# Start der großen Monatsschleife
for m in monate_config:
    monat_filter = (df_timeline.index >= m["start"]) & (df_timeline.index <= m["end"])
    woche_filter = (df_timeline.index >= m["woche_start"]) & (df_timeline.index <= m["woche_end"])

    df_woche = df_timeline[woche_filter]
    df_monat = df_timeline[monat_filter]

    # -----------------------------------------------------------------
    # PLOT A: Wochenverlauf
    # -----------------------------------------------------------------
    fig, ax = plt.subplots(figsize=(10, 6), dpi=300)

    ax.plot(df_woche.index, df_woche["Abregelung_MWh"],
            label="Ursprüngliche Abregelung (Ohne E-Autos)", color="crimson", linewidth=2.5, alpha=0.9)

    for name, res in ergebnisse_szenarien.items():
        style = sz_styles[name]
        ax.plot(res[woche_filter].index, res[woche_filter]["Verbleibende_Abregelung_MWh"],
                label=f"Verbleibend: {style['label_clean']}",
                linestyle=style["linestyle"],
                color=style["color"],
                linewidth=style["linewidth"])

    ax.set_title(f"Musterwoche {m['name']} 2025: Szenarienvergleich der Netz-Kompensation", fontsize=14,
                 fontweight='bold', pad=15)
    ax.set_ylabel("Abgeregelte Energie [MWh]", fontsize=12)
    ax.set_xlabel("Datum", fontsize=12)
    ax.set_xlim(df_woche.index.min(), df_woche.index.max())

    woche_ticks = df_woche.index[::24]
    labels_de = []
    for dt in woche_ticks:
        eng_day = dt.strftime('%a')
        de_day = wochentage_de.get(eng_day, eng_day)
        labels_de.append(dt.strftime(f'{de_day}, %d.%b'))

    ax.set_xticks(woche_ticks)
    ax.set_xticklabels(labels_de, rotation=15, ha='right')
    ax.legend(loc="upper left", frameon=True, facecolor="white", edgecolor="none", fontsize=11)
    plt.tight_layout()

    # ÄNDERUNG: Speichert das Bild dynamisch mit dem Monatsnamen im Dateinamen
    plt.savefig(f"plot_wochenverlauf_{m['name']}.png", bbox_inches='tight')
    plt.close()

    # -----------------------------------------------------------------
    # PLOT B: Monats-Doppelplot
    # -----------------------------------------------------------------
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(15, 10), sharex=True, dpi=300)

    res_realistisch_monat = ergebnisse_szenarien["Szenario 2: Realistisch"][monat_filter]
    abregelung_monat = df_monat["Abregelung_MWh"]

    ax1.fill_between(res_realistisch_monat.index, res_realistisch_monat["Verbleibende_Abregelung_MWh"],
                     color="crimson", alpha=0.6, label="Verbleibende Netz-Abregelung")
    ax1.fill_between(res_realistisch_monat.index, res_realistisch_monat["Verbleibende_Abregelung_MWh"],
                     abregelung_monat,
                     color="mediumseagreen", alpha=0.6,
                     label="Durch E-Autos kompensierte Energie (Szenario 2 Realistisch)")
    ax1.set_title(f"Monatsbilanz {m['name']} 2025: Netz-Entlastung", fontsize=13, fontweight='bold')
    ax1.set_ylabel("Energie [MWh]", fontsize=11)
    ax1.legend(loc="upper right", frameon=True, facecolor="white")

    for name, res in ergebnisse_szenarien.items():
        style = sz_styles[name]
        ax2.plot(res[monat_filter].index, res[monat_filter]["SoC_Flotte"] * 100,
                 label=f"Ladestand: {style['label_clean']}",
                 color=style["color"],
                 linestyle="-",
                 alpha=0.85,
                 linewidth=2.5)

    ax2.set_title(f"Flotten-Ladestand (SoC) über den gesamten Monat {m['name']}", fontsize=12, fontweight='bold')
    ax2.set_ylabel("Flotten-SoC [%]", fontsize=11)
    ax2.set_xlabel("Datum", fontsize=11)
    ax2.set_ylim(20, 100)
    ax2.legend(loc="upper left", frameon=True, facecolor="white")
    ax2.set_xlim(df_monat.index.min(), df_monat.index.max())

    ax2.xaxis.set_major_formatter(mdates.DateFormatter('%d.%b'))
    ax2.xaxis.set_major_locator(mdates.DayLocator(interval=3))
    plt.tight_layout()

    # ÄNDERUNG: Speichert das Bild dynamisch mit dem Monatsnamen im Dateinamen
    plt.savefig(f"plot_monatsbilanz_{m['name']}.png", bbox_inches='tight')
    plt.close()

print("\n[INFO] Alle Grafiken wurden erfolgreich exportiert:")
print(" - plot_szenarienvergleich.png")
for m in monate_config:
    print(f" - plot_wochenverlauf_{m['name']}.png")
    print(f" - plot_monatsbilanz_{m['name']}.png")