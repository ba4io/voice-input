# -*- coding: utf-8 -*-
"""加词工具：填入听到的错词和想要的正确词，追加到 hotwords.txt。"""
import os
import tkinter as tk
from tkinter import simpledialog, messagebox

BASE = os.path.dirname(os.path.abspath(__file__))
HW = os.path.join(BASE, "hotwords.txt")


def main():
    root = tk.Tk()
    root.withdraw()

    wrong = simpledialog.askstring("加词 1/2", "听到的错词：", parent=root)
    if not wrong or not wrong.strip():
        return
    correct = simpledialog.askstring("加词 2/2", "你想要的正确写法：", parent=root)
    if not correct or not correct.strip():
        return
    wrong, correct = wrong.strip(), correct.strip()
    if wrong == correct:
        messagebox.showinfo("加词", "两个词一样，什么都没做")
        return

    lines = []
    if os.path.exists(HW):
        with open(HW, encoding="utf-8") as f:
            lines = f.read().splitlines()

    merged = False
    for i, line in enumerate(lines):
        if not line.strip() or line.strip().startswith("#"):
            continue
        parts = [x.strip() for x in line.split("|")]
        if parts and parts[0] == correct:
            if wrong not in parts[1:]:
                lines[i] = line.rstrip() + "|" + wrong
            merged = True
            break

    if not merged:
        lines.append("%s|%s" % (correct, wrong))

    with open(HW, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")

    messagebox.showinfo("加词", "已加入：%s → %s\n下次识别立即生效" % (wrong, correct))


main()
