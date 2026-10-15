# Wyświetlanie ofert z rankingu modelu

Otwórz plik `best_offers.py` w głównym folderze projektu. Możesz uruchomić go przyciskiem Run w VS Code, wybierając środowisko Python projektu. W terminalu otwartym w tym folderze działa też:

```powershell
python best_offers.py
```

Program wyświetla do pięciu kandydatów: cenę z ogłoszenia, przewidywaną cenę ofertową, różnicę procentową, dzielnicę, metraż, pokoje, czas ostatniego odczytu i pełny link do źródła. Pierwszy wynik ma największą procentową różnicę względem modelu spośród ofert spełniających filtry.

Żeby pokazać tylko pierwszy wynik:

```powershell
python best_offers.py --top 1
```

Odpowiednik uruchomienia przez moduł:

```powershell
python -m src.show_deals --top 5
```

## Skąd pochodzą wyniki

Domyślne wejście to `data/deals.csv`, zapisywane przez `src.train` po treningu i ocenie modelu. Możesz wybrać inny plik rankingu:

```powershell
python best_offers.py --input data/deals.csv --top 10
```

**Na 15 października 2026 projekt nie ma jeszcze rzeczywistego wytrenowanego modelu ani wygenerowanego `deals.csv`.** W tej sytuacji program wyjaśnia, którego pliku brakuje, i kończy działanie kodem 2. Katalog pobranych ofert oraz wynik audytu jakości nie zastępują rankingu. Program nie generuje przykładowych „okazji” ani nie uruchamia treningu samodzielnie.

Wymagane kolumny rankingu to `source`, `listing_id`, `url`, `city`, `price_pln`, `area_m2`, `rooms`, `observed_at`, `predicted_price_pln`, `is_deal`, `seen_in_training` i `partition`. Istniejące `src.train.score_listings()` zapisuje te pola. Dodatkowe cechy, np. dzielnica i piętro, są zachowywane. Różnicę procentową viewer oblicza ponownie z ceny i predykcji.

## Jak wybieramy widoczne oferty

Program sprawdza najnowszy odczyt każdej pary źródło–ID. Nie wraca do wcześniejszej niższej ceny, jeśli nowszy rekord nie spełnia wymagań. Niejednoznaczne remisy i niepewne daty wyłączają tożsamość z tego widoku.

Widoczne rekordy muszą przejść aktualny audyt jakości, mieć dodatnią, skończoną predykcję, poprawny link OLX lub Otodom oraz potwierdzony czas ze strefą czasową. Wykluczamy rekordy oznaczone jako użyte w treningu i rekordy z nieznanym albo sprzecznym opisem podziału danych. Domyślnie czas odczytu ma najwyżej 7 dni, a różnica względem modelu wynosi co najmniej 15%:

```text
(przewidywana cena ofertowa − cena ogłoszenia) / przewidywana cena ofertowa
```

Parametry można podać jawnie:

```powershell
python best_offers.py --top 5 --max-age-days 2 --discount-threshold 20
```

`--discount-threshold 20` oznacza próg 20%. `--top` przyjmuje wartości 1–50. Program informuje, ile ofert przeszło filtry; brak wyniku jest poprawnym rezultatem, jeśli żadna oferta ich nie spełnia.

Filtry dotyczą wyłącznie wierszy obecnych w CSV. Obniżenie progu w wyświetlaczu nie przywraca ofert odrzuconych wcześniej podczas generowania `deals.csv`.

To ranking różnic względem modelu ceny ofertowej. Jakość modelu, cechy pominięte w danych oraz niepotwierdzona tożsamość mieszkań między portalami wpływają na jego interpretację. Czas odczytu nie potwierdza bieżącej dostępności; przed oceną kandydata sprawdź oryginalny link. Wyświetlacz nie łączy duplikatów i nie rozstrzyga o wartości transakcyjnej mieszkania.

Kod czyta lokalny CSV. Zachowuje plik wejściowy, bazę i model; nie otwiera stron ofert automatycznie i nie wymaga `.env`.
