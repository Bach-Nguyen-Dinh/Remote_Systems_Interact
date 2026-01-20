#include <stdio.h>
#include <unistd.h>

#include "i2c.h"
#include "iim42652.h"


struct I2cDevice dev;


int iim42652_avail(void)
{
	int rc = -1;
	if (i2c_read_reg(&dev, IIM42652_REG_WHO_AM_I) == IIM42652_CHIP_ID) {
		iim42652_soft_reset();
		return 0;
	}
	return rc;
}


/*!
 *  @brief  Exit IDLE mode. The RC oscillator is powered on even if Accel and Gyro are powered off.
 *  @param  NULL.
 *  @return NULL.
 */
void iim42652_ex_idle(void)
{
	uint8_t tmp;
	tmp = i2c_read_reg(&dev, IIM42652_REG_PWR_MGMT0);
	tmp |= ~0xEF;
	i2c_write_reg(&dev, IIM42652_REG_PWR_MGMT0, tmp);
}

/*!
 *  @brief  IDLE mode. When Accel and Gyro are powered off.
            The chip will go to OFF state, since the RC oscillator will also be powered off.
 *  @param  NULL.
 *  @return NULL.
 */
void iim42652_idle(void)
{
	uint8_t tmp;
	tmp = i2c_read_reg(&dev, IIM42652_REG_PWR_MGMT0);
	tmp &= 0xEF;
	i2c_write_reg(&dev, IIM42652_REG_PWR_MGMT0, tmp);
}

void iim42652_soft_reset(void)
{
	uint8_t tmp;
	tmp = i2c_read_reg(&dev, IIM42652_REG_DEVICE_CONFIG);
	tmp |= 0x01;
	i2c_write_reg(&dev, IIM42652_REG_DEVICE_CONFIG, tmp);
	usleep(1000 * 10);//10ms
}


/*!
 *  @brief  Enable IIM42652 gyroscope measurement.
 *  @param  NULL.
 *  @return NULL.
 */
void iim42652_gyroscope_enable(void)
{
	uint8_t tmp;
	i2c_readn_reg(&dev, IIM42652_REG_PWR_MGMT0, &tmp, 1);
	tmp |= IIM42652_SET_GYRO_TLOW_NOISE_MODE;
	i2c_writen_reg(&dev, IIM42652_REG_PWR_MGMT0, &tmp, 1);
}

/*!
 *  @brief  Disable IIM42652 gyroscope measurement.
 *  @param  NULL.
 *  @return NULL.
 */
void iim42652_gyroscope_disable(void)
{
	uint8_t tmp;
	i2c_readn_reg(&dev, IIM42652_REG_PWR_MGMT0, &tmp, 1);
	tmp &= 0xF3;
	i2c_writen_reg(&dev, IIM42652_REG_PWR_MGMT0, &tmp, 1);
}

/*!
 *  @brief  Enable IIM42652 acceleration measurement.
 *  @param  NULL.
 *  @return NULL.
 */
void iim42652_accelerometer_enable(void)
{
	uint8_t tmp;
	i2c_readn_reg(&dev, IIM42652_REG_PWR_MGMT0, &tmp, 1);
	tmp |= IIM42652_SET_ACCEL_LOW_NOISE_MODE;
	i2c_writen_reg(&dev, IIM42652_REG_PWR_MGMT0, &tmp, 1);
}

/*!
 *  @brief  Disable IIM42652 acceleration measurement.
 *  @param  NULL.
 *  @return NULL.
 */
void iim42652_accelerometer_disable(void)
{
	uint8_t tmp;
	i2c_readn_reg(&dev, IIM42652_REG_PWR_MGMT0, &tmp, 1);
	tmp &= 0xFC;
	i2c_writen_reg(&dev, IIM42652_REG_PWR_MGMT0, &tmp, 1);
}

/*!
 *  @brief  Enable IIM42652 temperature measurement.
 *  @param  NULL.
 *  @return NULL.
 */
void iim42652_temperature_enable(void)
{
	uint8_t tmp;
	i2c_readn_reg(&dev, IIM42652_REG_PWR_MGMT0, &tmp, 1);
	tmp &= ~IIM42652_SET_TEMPERATURE_DISABLED;
	i2c_writen_reg(&dev, IIM42652_REG_PWR_MGMT0, &tmp, 1);
}

/*!
 *  @brief  Disable IIM42652 temperature measurement.
 *  @param  NULL.
 *  @return NULL.
 */
void iim42652_temperature_disable(void)
{
	uint8_t tmp;
	i2c_readn_reg(&dev, IIM42652_REG_PWR_MGMT0, &tmp, 1);
	tmp |= IIM42652_SET_TEMPERATURE_DISABLED;
	i2c_writen_reg(&dev, IIM42652_REG_PWR_MGMT0, &tmp, 1);
}

/*!
 *  @brief  Register bank selection.
 *  @param  bank_sel	: Selected bank. eg: IIM42652_SET_BANK_0.
 *  @return NULL.
 */
void iim42652_bank_selection(uint8_t bank_sel)
{
	uint8_t tmp;
	i2c_writen_reg(&dev, IIM42652_REG_BANK_SEL, &bank_sel, 1);
	i2c_readn_reg(&dev, IIM42652_REG_BANK_SEL, &tmp, 1);
	//printf("bank_selection bank_sel = %d \n", tmp);
	usleep(1 * 1000);
}


/*!
 *  @brief  Get IIM42652 acceleration data.
 *  @param  accel_data	:Pointer to data of type @ IIM42652_axis_t.
 *  @return NULL.
 */
void iim42652_get_accel_data(IIM42652_axis_t *accel_data)
{
	uint8_t rx_buf[6];
	uint16_t tmp;

	i2c_readn_reg(&dev, IIM42652_REG_ACCEL_DATA_X1_UI, rx_buf, 6);

	tmp = rx_buf[0];
	tmp <<= 8;
	tmp |= rx_buf[1];

	accel_data->x = (int16_t)tmp;

	tmp = rx_buf[2];
	tmp <<= 8;
	tmp |= rx_buf[3];

	accel_data->y = (int16_t)tmp;

	tmp = rx_buf[4];
	tmp <<= 8;
	tmp |= rx_buf[5];

	accel_data->z = (int16_t)tmp;
}

/*!
 *  @brief  Get IIM42652 gyroscope data.
 *  @param  gyro_data	:Pointer to data of type @ IIM42652_axis_t.
 *  @return NULL.
 */
void iim42652_get_gyro_data(IIM42652_axis_t *gyro_data)
{
	uint8_t rx_buf[6];
	uint16_t tmp;

	i2c_readn_reg(&dev, IIM42652_REG_GYRO_DATA_X1_UI, rx_buf, 6);

	tmp = rx_buf[0];
	tmp <<= 8;
	tmp |= rx_buf[1];

	gyro_data->x = (int16_t)tmp;

	tmp = rx_buf[2];
	tmp <<= 8;
	tmp |= rx_buf[3];

	gyro_data->y = (int16_t)tmp;

	tmp = rx_buf[4];
	tmp <<= 8;
	tmp |= rx_buf[5];

	gyro_data->z = (int16_t)tmp;
}

/*!
 *  @brief  Get IIM42652 temperature data.
 *  @param  temperature	:Pointer to temperature variable.
 *  @return NULL.
 */
void iim42652_get_temperature(float *temperature)
{
	uint8_t rx_buf[2];
	int16_t tmp;

	i2c_readn_reg(&dev, IIM42652_REG_TEMP_DATA1_UI, rx_buf, 2);

	tmp = rx_buf[0];
	tmp <<= 8;
	tmp |= rx_buf[1];

	*temperature = (float)tmp;
	*temperature /= 132.48;
	*temperature += 25;
}

/*!
 *  @brief  Set IIM42652 ACC full scale range.
 *  @param  accel_fsr_g	:Full scal range value. Reference@ IIM42652_ACCEL_CONFIG0_FS_SEL_t.
 *  @return NULL.
 */
void iim42652_set_accel_fsr(IIM42652_ACCEL_CONFIG0_FS_SEL_t accel_fsr_g)
{
	uint8_t accel_cfg_0_reg;
	i2c_readn_reg(&dev, IIM42652_REG_ACCEL_CONFIG0, &accel_cfg_0_reg, 1);
	accel_cfg_0_reg &= (uint8_t)~BIT_ACCEL_CONFIG0_FS_SEL_MASK;
	accel_cfg_0_reg |= (uint8_t)accel_fsr_g;
	i2c_writen_reg(&dev, IIM42652_REG_ACCEL_CONFIG0, &accel_cfg_0_reg, 1);
}

/*!
 *  @brief  Set IIM42652 ACC output data rate.
 *  @param  frequency	:Output data rate value. Reference@ IIM42652_ACCEL_CONFIG0_ODR_t.
 *  @return NULL.
 */
void iim42652_set_accel_frequency(const IIM42652_ACCEL_CONFIG0_ODR_t frequency)
{
	uint8_t accel_cfg_0_reg;
	i2c_readn_reg(&dev, IIM42652_REG_ACCEL_CONFIG0, &accel_cfg_0_reg, 1);
	accel_cfg_0_reg &= (uint8_t)~BIT_ACCEL_CONFIG0_ODR_MASK;
	accel_cfg_0_reg |= (uint8_t)frequency;
	i2c_writen_reg(&dev, IIM42652_REG_ACCEL_CONFIG0, &accel_cfg_0_reg, 1);
}

/*!
 *  @brief  Set IIM42652 GYRO full scale range.
 *  @param  gyro_fsr_dps	:Full scal range value. Reference@ IIM42652_GYRO_CONFIG0_FS_SEL_t.
 *  @return NULL.
 */
void iim42652_set_gyro_fsr(IIM42652_GYRO_CONFIG0_FS_SEL_t gyro_fsr_dps)
{
	uint8_t gyro_cfg_0_reg;
	i2c_readn_reg(&dev, IIM42652_REG_GYRO_CONFIG0, &gyro_cfg_0_reg, 1);
	gyro_cfg_0_reg &= (uint8_t)~BIT_GYRO_CONFIG0_FS_SEL_MASK;
	gyro_cfg_0_reg |= (uint8_t)gyro_fsr_dps;
	i2c_writen_reg(&dev, IIM42652_REG_GYRO_CONFIG0, &gyro_cfg_0_reg, 1);
}

/*!
 *  @brief  Set IIM42652 GYRO output data rate.
 *  @param  frequency	:Output data rate value. Reference@ IIM42652_GYRO_CONFIG0_ODR_t.
 *  @return NULL.
 */
void iim42652_set_gyro_frequency(const IIM42652_GYRO_CONFIG0_ODR_t frequency)
{
	uint8_t gyro_cfg_0_reg;
	i2c_readn_reg(&dev, IIM42652_REG_GYRO_CONFIG0, &gyro_cfg_0_reg, 1);
	gyro_cfg_0_reg &= (uint8_t)~BIT_GYRO_CONFIG0_ODR_MASK;
	gyro_cfg_0_reg |= (uint8_t)frequency;
	i2c_writen_reg(&dev, IIM42652_REG_GYRO_CONFIG0, &gyro_cfg_0_reg, 1);
}

/*!
 *  @brief  Get WOM interrupt flag, clears on read.
 *  @param  NULL.
 *  @return Return the value in register INT_STATUS2.
 *			For details, refer to the description of INT_STATUS2 in the date sheet.
 */
uint8_t iim42652_get_WOM_INT(void)
{
	uint8_t data;
	i2c_readn_reg(&dev, IIM42652_REG_INT_STATUS2, &data, 1);
	//printf("get_WOM_INT IIM42652_REG_INT_STATUS2 = %d\n", data);
	return data;
}


int iim42652_open(void)
{
	int rc = -1;

	/*
	 * Set the I2C bus filename and slave address,
	 */
	dev.filename = "/dev/i2c-1"; //TODO: Change the bus no based on the board & i2c bus you choose
	dev.addr = IIM42652_SET_DEV_ADDR;

	/*
	 * Start the I2C device.
	 */
	rc = i2c_start(&dev);
	if (rc) {
		printf("Failed to start I2C device\n");
		return rc;
	}
	return 0;
}

void iim42652_close(void)
{
	i2c_stop(&dev);
}
