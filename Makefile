CC = gcc
CFLAGS = -I fusion_6dofs/dependencies/Fusion -Wall -Wextra
LDFLAGS = -lm

SRC = main.c \
      sensor_iim42652/i2c.c \
      sensor_iim42652/iim42652.c \
      fusion_6dofs/dependencies/FusionAhrs.c \
      fusion_6dofs/dependencies/FusionBias.c \
      fusion_6dofs/dependencies/FusionCompass.c

OUT = main

all: $(OUT)

$(OUT): $(SRC)
	$(CC) $(SRC) $(CFLAGS) $(LDFLAGS) -o $(OUT)

clean:
	rm -f $(OUT)
