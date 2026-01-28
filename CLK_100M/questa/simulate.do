onbreak {quit -f}
onerror {quit -f}

vsim -t 1ps -lib xil_defaultlib CLK_100M_opt

do {wave.do}

view wave
view structure
view signals

do {CLK_100M.udo}

run -all

quit -force
