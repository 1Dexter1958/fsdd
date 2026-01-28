onbreak {quit -force}
onerror {quit -force}

asim -t 1ps +access +r +m+CLK_100M -L xpm -L xil_defaultlib -L unisims_ver -L unimacro_ver -L secureip -O5 xil_defaultlib.CLK_100M xil_defaultlib.glbl

do {wave.do}

view wave
view structure

do {CLK_100M.udo}

run -all

endsim

quit -force
