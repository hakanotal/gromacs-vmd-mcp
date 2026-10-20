"""Tcl scripts run in VMD's text mode.

Ported from the prototype's VMDTemplates (load trajectory, representation, render, RMSD,
contacts, movie), with one change that matters: the scripts are fixed text. Values arrive as
$argv elements and are used as Tcl variables, so a file name or selection can never become Tcl
code. vmd.wrap_script runs each one as a single catch block and appends `quit`.

Representations travel as trailing $argv triples: selection representation coloring.
"""

from typing import Dict, List

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

# A scene: representations from the triples, then the view. In text mode `display resize` gives empty
# images (VMD 2.0.0a7), so the size is set later by Tachyon (-res) and the zoom keeps the scene inside it.
_SCENE = r"""
proc set_scene {mol reps zoom} {
    mol delrep 0 $mol
    foreach {seltext rep coloring} $reps {
        mol selection $seltext
        mol representation $rep
        mol color $coloring
        mol addrep $mol
    }
    axes location off
    display projection Orthographic
    display resetview
    scale by $zoom
}
"""

# argv: structure trajectory frame mode zoom out [selection representation coloring]...
# mode "scene" writes a Tachyon scene file (rendered by the server at any size); "internal" writes a
# 512 x 512 TGA with VMD's built-in Tachyon.
RENDER = _LOAD + _SCENE + r"""
lassign $argv structure trajectory frame mode zoom out
set mol [load_files $structure $trajectory]
set_scene $mol [lrange $argv 6 end] $zoom
if {$frame < 0} {
    animate goto end
} else {
    animate goto $frame
}
if {$mode eq "scene"} {
    render Tachyon $out
} else {
    render TachyonInternal $out
}
puts "VMD_MCP_DONE frames=[molinfo $mol get numframes]"
"""

# argv: structure trajectory first last stride rotate max_frames mode zoom outdir [reps]...
# Writes frame_00000.dat (scene) or frame_00000.tga (internal) per rendered frame into outdir.
MOVIE = _LOAD + _SCENE + r"""
lassign $argv structure trajectory first last stride rotate max_frames mode zoom outdir
set mol [load_files $structure $trajectory]
set_scene $mol [lrange $argv 10 end] $zoom
set n [molinfo $mol get numframes]
if {$last < 0 || $last >= $n} { set last [expr {$n - 1}] }
set k 0
for {set i $first} {$i <= $last} {incr i $stride} {
    if {$k >= $max_frames} {
        puts "VMD_MCP_TRUNCATED at $max_frames frames"
        break
    }
    animate goto $i
    if {$rotate != 0} { rotate y by $rotate }
    if {$mode eq "scene"} {
        render Tachyon [file join $outdir [format "frame_%05d.dat" $k]]
    } else {
        render TachyonInternal [file join $outdir [format "frame_%05d.tga" $k]]
    }
    incr k
}
puts "VMD_MCP_DONE frames=$k of=$n"
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

# argv: structure trajectory selection output
RGYR = _LOAD + r"""
lassign $argv structure trajectory seltext out
set mol [load_files $structure $trajectory]
set sel [atomselect $mol $seltext]
set n [molinfo $mol get numframes]
set fh [open $out w]
for {set i 0} {$i < $n} {incr i} {
    $sel frame $i
    puts $fh "$i [measure rgyr $sel]"
}
close $fh
puts "VMD_MCP_DONE frames=$n atoms=[$sel num]"
"""

# argv: structure trajectory selection probe_radius restrict output  (restrict "none" for the whole selection)
SASA = _LOAD + r"""
lassign $argv structure trajectory seltext probe restrict out
set mol [load_files $structure $trajectory]
set sel [atomselect $mol $seltext]
if {$restrict ne "none"} { set rsel [atomselect $mol $restrict] }
set n [molinfo $mol get numframes]
set fh [open $out w]
for {set i 0} {$i < $n} {incr i} {
    $sel frame $i
    if {$restrict ne "none"} {
        $rsel frame $i
        set area [measure sasa $probe $sel -restrict $rsel]
    } else {
        set area [measure sasa $probe $sel]
    }
    puts $fh "$i $area"
}
close $fh
puts "VMD_MCP_DONE frames=$n"
"""

# argv: structure trajectory donor_selection acceptor_selection distance angle output
HBONDS = _LOAD + r"""
lassign $argv structure trajectory dontext acctext dist angle out
set mol [load_files $structure $trajectory]
set don [atomselect $mol $dontext]
set acc [atomselect $mol $acctext]
set n [molinfo $mol get numframes]
set fh [open $out w]
for {set i 0} {$i < $n} {incr i} {
    $don frame $i
    $acc frame $i
    set hb [measure hbonds $dist $angle $don $acc]
    puts $fh "$i [llength [lindex $hb 0]]"
}
close $fh
puts "VMD_MCP_DONE frames=$n"
"""

# argv: structure trajectory selection output. One line per frame: frame and one STRIDE letter per atom
# of the selection (use one atom per residue, for example name CA).
SECONDARY_STRUCTURE = _LOAD + r"""
lassign $argv structure trajectory seltext out
set mol [load_files $structure $trajectory]
set sel [atomselect $mol $seltext]
set n [molinfo $mol get numframes]
set fh [open $out w]
for {set i 0} {$i < $n} {incr i} {
    animate goto $i
    mol ssrecalc $mol
    $sel update
    puts $fh "$i [join [$sel get structure] {}]"
}
close $fh
puts "VMD_MCP_DONE frames=$n residues=[$sel num]"
"""

VERSION = r"""
puts "VMD_MCP_VERSION [vmdinfo version]"
"""

# Style presets: lists of representations (ported from the prototype's VISUALIZATION_STYLES, plus scenes
# with several representations)
_CARTOON = {"selection": "protein", "representation": "NewCartoon", "coloring": "Structure"}
STYLES: Dict[str, List[Dict[str, str]]] = {
    "protein_cartoon": [_CARTOON],
    "protein_surface": [{"selection": "protein", "representation": "QuickSurf", "coloring": "ResType"}],
    "ligand_licorice": [{"selection": "not protein and not water and not ions", "representation": "Licorice",
                         "coloring": "Name"}],
    "water_points": [{"selection": "water", "representation": "Points", "coloring": "Name"}],
    "all_lines": [{"selection": "all", "representation": "Lines", "coloring": "Name"}],
    "all_vdw": [{"selection": "all", "representation": "VDW", "coloring": "Name"}],
    "protein_ligand": [_CARTOON, {"selection": "not protein and not water and not ions",
                                  "representation": "Licorice", "coloring": "Name"}],
    "solvated": [_CARTOON, {"selection": "water and within 8 of protein", "representation": "Points",
                            "coloring": "Name"}],
}

DESCRIPTIONS = {
    "render_image": "Render a structure (or a trajectory frame) to an image, headless, at any size with Tachyon",
    "render_movie": "Render trajectory frames (optionally rotating) and assemble an .mp4 with ffmpeg",
    "rmsd_over_trajectory": "RMSD per frame of a selection against a reference frame, after fitting",
    "contacts_over_trajectory": "Number of atom pairs within a cutoff between two selections, per frame",
    "radius_of_gyration_over_trajectory": "Radius of gyration of a selection per frame",
    "sasa_over_trajectory": "Solvent accessible surface area of a selection per frame",
    "hbonds_over_trajectory": "Number of hydrogen bonds between donor and acceptor selections per frame",
    "secondary_structure_over_trajectory": "STRIDE secondary structure per residue and frame, with fractions",
}
