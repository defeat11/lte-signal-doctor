import os
import sys
import csv
import io
from rich.console import Console
from rich.table import Table

# Force UTF-8 encoding for Windows terminal
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

HISTORY_FILE = "modem_history.csv"
console = Console()

def main():
    if not os.path.exists(HISTORY_FILE):
        console.print("[red]❌ لا يوجد ملف سجل تاريخي بعد. يرجى تشغيل برنامج modem_analyzer.py أولاً لجمع البيانات.[/]")
        return

    try:
        with open(HISTORY_FILE, mode='r', encoding='utf-8') as f:
            reader = csv.reader(f)
            header = next(reader)
            rows = list(reader)
            
        if not rows:
            console.print("[yellow]⚠️ ملف السجل التاريخي فارغ.[/]")
            return
            
        # Get last 10 rows
        last_rows = rows[-10:]
        
        table = Table(title=f"آخر 10 قراءات مسجلة في السجل التاريخي ({HISTORY_FILE})", expand=True)
        
        # Add columns
        table.add_column("الوقت (Timestamp)", style="cyan", justify="center")
        table.add_column("RSRP 4G", style="yellow", justify="center")
        table.add_column("SINR 4G", style="yellow", justify="center")
        table.add_column("RSRP 5G", style="green", justify="center")
        table.add_column("SINR 5G", style="green", justify="center")
        table.add_column("البرج (Cell ID)", style="magenta", justify="center")
        table.add_column("الحالة (Status)", style="blue", justify="center")
        
        for row in last_rows:
            # Map columns by index:
            # 0: Timestamp, 1: LTE_RSRP, 2: LTE_RSRQ, 3: LTE_SINR, 4: LTE_RSSI, 5: LTE_CellID, 6: LTE_Band
            # 7: NR_RSRP, 8: NR_RSRQ, 9: NR_SINR, 10: NR_Bandwidth, 11: NR_Frequency, 12: Status
            timestamp = row[0]
            lte_rsrp = row[1]
            lte_sinr = row[3]
            nr_rsrp = row[7]
            nr_sinr = row[9]
            cell_id = row[5]
            status = row[12] if len(row) > 12 else "N/A"
            
            table.add_row(timestamp, lte_rsrp, lte_sinr, nr_rsrp, nr_sinr, cell_id, status)
            
        console.print(table)
    except Exception as e:
        console.print(f"[red]❌ حدث خطأ أثناء قراءة ملف السجل: {e}[/]")

if __name__ == "__main__":
    main()
