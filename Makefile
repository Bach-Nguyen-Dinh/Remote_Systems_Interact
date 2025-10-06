all:
	gcc i2c.c iim42652.c main.c -o iim42652

clean:
	rm iim42652 > /dev/null 2>&1
