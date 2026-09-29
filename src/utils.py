"""Utility functions for UI and file dialogs."""

import argparse


def pick_input_path_from_dialog() -> str | None:
    """Open file picker dialog for selecting COCO zip or folder."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:
        return None

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    root.lift()
    root.focus_force()

    selected = filedialog.askopenfilename(
        title="Select COCO export zip",
        filetypes=[("Zip archives", "*.zip"), ("All files", "*.*")],
    )

    root.destroy()
    return selected if selected else None


def show_info_popup(message: str, title: str = "DFET Annotation tool") -> None:
    """Display info popup message."""
    try:
        import tkinter as tk
        from tkinter import messagebox
    except Exception:
        print(message)
        return

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    root.lift()
    root.focus_force()
    messagebox.showinfo(title, message, parent=root)
    root.destroy()


def parse_arguments() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Review COCO polygon and bbox annotations from a zip export or directory."
    )
    parser.add_argument(
        "input_path",
        nargs="?",
        default=None,
        help="Path to COCO zip export or extracted folder. If omitted, a file picker opens.",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run non-GUI checks only and exit.",
    )
    return parser.parse_args()
