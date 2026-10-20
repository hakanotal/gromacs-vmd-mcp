"""Tcl scripts run in VMD's text mode.

Ported from the prototype's VMDTemplates (load trajectory, representation, render, RMSD,
contacts), with one change that matters: the scripts are fixed text. Values arrive as $argv
elements and are used as Tcl variables, so a file name or selection can never become Tcl code.
vmd.wrap_script runs each one as a single catch block and appends `quit`.
"""

from typing import Dict

# argv: structure trajectory  ("none" when there is no trajectory)
_LOAD = r"""
proc load_files {structure trajectory} {
    set mol [mol new $structure waitfor all]
    if {$trajectory ne "none"} {
        mol addfile $trajectory waitfor all molid $mol
    }
    return $mol
}
"""

# argv: structure trajectory selection reference_frame align output
RMSD = _LOAD + r"""
lassign $argv structure trajectory seltext ref align out
set mol [load_files $structure $trajectory]
set sel [atomselect $mol $seltext]
set refsel [atomselect $mol $seltext frame $ref]
set all [atomselect $mol all]
set n [molinfo $mol get numframes]
set fh [open $out w]
for {set i 0} {$i < $n} {incr i} {
    $sel frame $i
    $all frame $i
    if {$align} { $all move [measure fit $sel $refsel] }
    puts $fh "$i [measure rmsd $sel $refsel]"
}
close $fh
puts "VMD_MCP_DONE frames=$n atoms=[$sel num]"
"""

# argv: structure trajectory selection1 selection2 cutoff output
CONTACTS = _LOAD + r"""
lassign $argv structure trajectory seltext1 seltext2 cutoff out
set mol [load_files $structure $trajectory]
set sel1 [atomselect $mol $seltext1]
set sel2 [atomselect $mol $seltext2]
set n [molinfo $mol get numframes]
set fh [open $out w]
for {set i 0} {$i < $n} {incr i} {
    $sel1 frame $i
    $sel2 frame $i
    set pairs [measure contacts $cutoff $sel1 $sel2]
    puts $fh "$i [llength [lindex $pairs 0]]"
}
close $fh
puts "VMD_MCP_DONE frames=$n"
"""

# argv: structure trajectory frame selection representation coloring output.tga
# No `display resize`: in text mode (VMD 2.0.0a7) it gives an empty image, so images are 512x512.
RENDER = _LOAD + r"""
lassign $argv structure trajectory frame seltext rep coloring out
set mol [load_files $structure $trajectory]
mol delrep 0 $mol
mol selection $seltext
mol representation $rep
mol color $coloring
mol addrep $mol
if {$frame < 0} {
    animate goto end
} else {
    animate goto $frame
}
axes location off
display projection Orthographic
display resetview
render TachyonInternal $out
puts "VMD_MCP_DONE frames=[molinfo $mol get numframes]"
"""

VERSION = r"""
puts "VMD_MCP_VERSION [vmdinfo version]"
"""

# Presets ported from the prototype's VISUALIZATION_STYLES
STYLES: Dict[str, Dict[str, str]] = {
    "protein_cartoon": {"selection": "protein", "representation": "NewCartoon", "coloring": "Structure"},
    "protein_surface": {"selection": "protein", "representation": "QuickSurf", "coloring": "ResType"},
    "ligand_licorice": {"selection": "not protein and not water", "representation": "Licorice", "coloring": "Name"},
    "water_points": {"selection": "water", "representation": "Points", "coloring": "Name"},
    "all_lines": {"selection": "all", "representation": "Lines", "coloring": "Name"},
}

DESCRIPTIONS = {
    "render_image": "Render a structure (and a trajectory frame) to an image with Tachyon, headless",
    "rmsd_over_trajectory": "RMSD per frame of a selection against a reference frame, after fitting",
    "contacts_over_trajectory": "Number of atom pairs within a cutoff between two selections, per frame",
}
