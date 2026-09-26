~ Little tool i made with Claude in 2 days so i could install some of my games
Before i get into technicals (what libs and stuff Claude chose) i just wanted to say : This is not meant to be a Big Mainstream polished App/Tool, this is something i created with Claude to fix a problem i've personally had

# Vibe-Coded-Linux-Multi-Disc-Installer

Disclaimer:
  This Tool is NOT affiliated with Valve and/or their Proton software
  I didn't vibe-code this for Piracy but for Discs i own

This Little Vibe-Coded Tool helps you install games that use more than 1 disc to install.
The Task i gave Claude was to take the disc and display it as a Drive Letter inside of Proton, it usually defaults to Z:/run/media... in my experience, most Disc-Installers i've used don't work with this, so I used Claude to take wherever the disc/mount actually lives and automatically map that to a fixed Drive Letter ("D:")

In my Experience this Solution works quite well
I have not tested it with .ISO and other Image Files, which is a Function that Claude implemented.

In the Application you can set 2 .exe Files, one for the Disc itself and one for the Game, there is also a "Swap Disc" Button that (in my Experience) is not neccesary, you can manually mount it in every File Manager yourself by just trying to open the disc in a File Manager. You can also open the Drive in a File Manager and the Prefix Folder directly from the GUI.
You can of course install multiple Games in the Application and you can select the Proton Version on a per-game basis.

Installation:
  Requirements : Python3 (3.10+), PySide6, Some version of Proton installed (, It also uses things that are installed on basically every major distro)
  Instructions :
    Install the Requirements :
      Arch : sudo pacman -S pyside6 python3 xdg-utils
        optional : Python Up-QT or ProtonPlus to install a Proton Version easily
      I imagine its similar for other Distros
    Download the script (proton_disc_gui.py)
    Run it with "python3 path/to/proton_disc_gui.py"

License:
  GPL 3.0
    -You can fork it and change it but you CAN'T take it closed source
