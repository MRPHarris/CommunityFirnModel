#!/usr/bin/python
# -*- coding: utf-8 -*-
'''
2/24/2021

This script takes a pandas dataframe containing climate data for a particular
site and generates climate histories to feed into CFM as forcing.

The script resamples the data to the specified time step (e.g. if you have 
hourly data and you want a daily run, it resamples to daily.)

At present, spin up is generated by just repeatsing the reference climate 
interval over and over again.

YOU MAY HAVE TO EDIT THIS SCRIPT A LOT TO MAKE IT WORK WITH YOUR FILE STRUCTURE 
AND WHAT CLIMATE FILES YOU HAVE.

And, for now there are little things you need to search out and change manually,
like the reference climate interval. Sorry!

@author: maxstev
'''

import numpy as np
from datetime import datetime, timedelta, date
import pandas as pd
import time
import calendar
import hl_analytic as hla
import cmath
import sys

def toYearFraction(date):
    '''
    convert datetime to decimal date 
    '''
    def sinceEpoch(date): # returns seconds since epoch
        return calendar.timegm(date.timetuple())
    s = sinceEpoch

    year = date.year
    startOfThisYear = datetime(year=year, month=1, day=1)
    startOfNextYear = datetime(year=year+1, month=1, day=1)

    yearElapsed = s(date) - s(startOfThisYear)
    yearDuration = s(startOfNextYear) - s(startOfThisYear)
    fraction = yearElapsed/yearDuration

    return date.year + fraction

def decyeartodatetime(din):
    start = din
    year = int(start)
    rem = start - year
    base = datetime(year, 1, 1)
    result = base + timedelta(seconds=(base.replace(year=base.year + 1) - base).total_seconds() * rem)
    result2 = result.replace(hour=0, minute=0, second=0, microsecond=0)
    return result

def effectiveT(T):
    '''
    The Arrhenius mean temperature.
    '''
    # Q   = -1 * 60.0e3
    Q   = -1 * 59500.0
    R   = 8.314
    k   = np.exp(Q/(R*T))
    km  = np.mean(k)
    return Q/(R*np.log(km))

def calcSEB(SWGNT,LWGAB,HFLUX,EFLUX,TS,tindex,dt,GHTSKIN=0,dz=0.05,rhos=400):
    '''
    general solver to calculate skin temperature and melt flux based on energy inputs.
    
    Note that HFLUX and EFLUX are additive here, which means that positive HFLUX and EFLUX mean
    energy going into the surface - this is opposite sign convention of MERRA2, so multiply those
    by -1 when doing MERRA2 calculations.
    
    MAR uses the positive flux = energy into surface convention. 
    
    dt is in seconds - time resolution of inputs, e.g. 1hour data, dt=3600
    
    outputs: 
    TcalcH [K]
    meltmassH [kg/m2/timestep] (where timestep is dt, so ends up being melt per time step in the data frame)
    '''
    
    SBC = 5.67e-8
    CP_I = 2097.0 
    m = rhos*dz
    LF_I = 333500.0 #[J kg^-1]
    flux_df1 = (SWGNT + LWGAB + HFLUX + EFLUX + GHTSKIN)

    TcalcH = np.zeros_like(flux_df1)
    meltmassH = np.zeros_like(flux_df1)

    dts = TS

    # tindex
    fqs = FQS()
    for kk,mdate in enumerate(tindex): #loop through all time steps, calculate melt for all lat/lon pairs at that time
        pmat = np.zeros((5)) # p matrix to put into FQS solver
        if kk==0:
            T_0 = dts[kk]
        else:
            T_0 = TcalcH[kk-1]

        a = SBC*dt/(CP_I*m)
        b = 0
        c = 0
        d = 1
        e = -1 * (flux_df1[kk]*dt/(CP_I*m)+T_0)

        pmat[0] = a
        pmat[3] = d
        pmat[4] = e
        pmat[np.isnan(pmat)] = 0

        r = fqs.quartic_roots(pmat)
        Tnew = (r[((np.isreal(r)) & (r>0))].real)
        Tnew[np.isnan(e)] = np.nan

        if Tnew>=273.15:
            TcalcH[kk] = 273.15000000000000        
            meltmassH[kk] = (flux_df1[kk] - SBC*273.15**4) / LF_I * dt #multiply by dt to put in units per time step

        else:
            try:
                TcalcH[kk] = Tnew
            except:
                print(kk)
                print(Tnew)
            meltmassH[kk] = 0
            
    return TcalcH,meltmassH

def makeSpinFiles(CLIM_name,timeres='1D',Tinterp='mean',spin_date_st = 1980.0, spin_date_end = 1995.0,melt=False,desired_depth = None,SEB=False,rho_bottom=916,calc_melt=False,num_reps=None):
    '''
    load a pandas dataframe, called df_CLIM, that will be resampled and then used 
    to create a time series of climate variables for spin up. 
    the index of must be datetimeindex for resampling.
    df_CLIM can have any number of columns: BDOT, TSKIN, SMELT, RAIN, 
    SUBLIM (use capital letters. We use SMELT because melt is a pandas function)
    Hopefully this makes it easy to adapt for the different climate products.

    UNITS FOR MASS FLUXES IN THE DATAFRAMES ARE kg/m^2 PER TIME STEP SIZE IN
    THE DATA FRAME. e.g. if you have hourly data in the dataframe, the units
    for accumulation are kg/m^2/hour - the mass of precip that fell during that 
    time interval.

    CFM takes units of m ice eq./year, so this script returns units in that 
    format.

    Parameters
    ----------

    timeres: pandas Timedelta (string)
        Resampling frequency, e.g. '1D' is 1 day; '1M' for 1 month.
    melt: boolean
        Whether or not the model run includes melt
    Tinterp: 'mean', 'effective', or 'weighted'
        how to resample the temperature; mean is regular mean, 'effective' is 
        Arrhenius mean; 'weighted' is accumulation-weighted mean
     spin_date_st: float
         decimal date of the start of the reference climate interval (RCI)
     spin_date_end: float
         decimal date of the end of the RCI

    Returns
    -------
    CD: dictionary
        Dictionary full of the inputs (time, SMB, temperature, etc.) that
        will force the CFM. Possible keys to have in the dictionary are: 'time',
        which is decimal date; 'TSKIN' (surface temperature), 'BDOT'
        (accumulation, m i.e.), 'SMELT' (snowmelt, m i.e.), and 'RAIN'. 
    StpsPerYr: float
        number of steps per year (mean) for the timeres you selected.
    depth_S1: float
        depth of the 550 kg m^-3 density horizon (or other density; you can pick)
        this is used for the regrid module
    depth_S2: float
        depth of the 750 kg m^-3 density horizon (or other density; you can pick)
        this is used for the regrid module
    desired_depth: float
        this is the depth you should set to be the bottom of the domain if you 
        want to model to 916 kg m^-3.
    '''

    SPY = 365.25*24*3600

    if type(CLIM_name) == str:
        df_CLIM = pd.read_pickle(CLIM_name)
    else: #CLIM_name is not a pickle, it is the dataframe being passed
        df_CLIM = CLIM_name

    if (not SEB and not calc_melt): # just use T_surf and melt from the input climate

        drn = {'TS':'TSKIN','EVAP':'SUBLIM'} #customize this to change your dataframe column names to match the required inputs
        try:
            df_CLIM['RAIN'] = df_CLIM['PRECTOT'] - df_CLIM['PRECSNO']
            df_CLIM['BDOT'] = df_CLIM['PRECSNO'] #+ df_CLIM['EVAP']
            # df_CLIM['SUBLIM'] = df_CLIM[]

        except:
            pass
        df_CLIM.rename(mapper=drn,axis=1,inplace=True)
        try:
            df_CLIM.drop(['EVAP','PRECTOT','PRECSNO'],axis=1,inplace=True)
        except:
            pass
        l1 = df_CLIM.columns.values.tolist()
        l2 = ['SMELT','BDOT','RAIN','TSKIN','SUBLIM','SRHO']
        notin = list(np.setdiff1d(l1,l2))
        df_CLIM.drop(notin,axis=1,inplace=True)
        # df_BDOT = pd.DataFrame(df_CLIM.BDOT)
        df_TS = pd.DataFrame(df_CLIM.TSKIN)

        res_dict_all = {'SMELT':'sum','BDOT':'sum','RAIN':'sum','TSKIN':'mean','SUBLIM':'sum','SRHO':'mean'} # resample type for all possible variables
        res_dict = {key:res_dict_all[key] for key in df_CLIM.columns} # resample type for just the data types in df_CLIM

        # df_BDOT_re = df_BDOT.resample(timeres).sum()
        if Tinterp == 'mean':
            df_TS_re = df_TS.resample(timeres).mean()
        elif Tinterp == 'effective':
            df_TS_re = df_TS.resample(timeres).apply(effectiveT)
        elif Tinterp == 'weighted':
            df_TS_re = pd.DataFrame(data=(df_BDOT.BDOT*df_TS.TSKIN).resample(timeres).sum()/(df_BDOT.BDOT.resample(timeres).sum()),columns=['TSKIN'])
            # pass

        df_CLIM_re = df_CLIM.resample(timeres).agg(res_dict)
        df_CLIM_re.TSKIN = df_TS_re.TSKIN
        df_CLIM_ids = list(df_CLIM_re.columns)

        df_CLIM_re['decdate'] = [toYearFraction(qq) for qq in df_CLIM_re.index]
        df_CLIM_re = df_CLIM_re.ffill()

        # df_TS_re['decdate'] = [toYearFraction(qq) for qq in df_TS_re.index]
        # df_BDOT_re['decdate'] = [toYearFraction(qq) for qq in df_BDOT_re.index]
        # df_TS_re = df_TS_re.fillna(method='pad')

        stepsperyear = 1/(df_CLIM_re.decdate.diff().mean())

        if 'SUBLIM' not in df_CLIM_re:
            df_CLIM_re['SUBLIM'] = np.zeros_like(df_CLIM_re['BDOT'])
            print('SUBLIM not in df_CLIM! (RCMpkl_to_spin.py, 232')

        BDOT_mean_IE = ((df_CLIM_re['BDOT']+df_CLIM_re['SUBLIM'])*stepsperyear/917).mean()
        T_mean = (df_TS_re['TSKIN']).mean()

        hh  = np.arange(0,501)
        age, rho = hla.hl_analytic(350,hh,T_mean,BDOT_mean_IE)    
        if not desired_depth:
            # desired_depth = hh[np.where(rho>=916)[0][0]]
            desired_depth = hh[np.where(rho>=rho_bottom)[0][0]]
            depth_S1 = hh[np.where(rho>=450)[0][0]]
            depth_S2 = hh[np.where(rho>=650)[0][0]]
        else:
            desired_depth = desired_depth
            depth_S1 = desired_depth * 0.5
            depth_S2 = desired_depth * 0.75
        
        #### Make spin up series ###
        RCI_length = spin_date_end-spin_date_st+1
        if num_reps is not None:
            pass
        else:
            num_reps = int(np.round(desired_depth/BDOT_mean_IE/RCI_length))
        years = num_reps*RCI_length
        sub = np.arange(-1*years,0,RCI_length)
        startyear = int(df_CLIM_re.index[0].year + sub[0])
        startmonth = df_CLIM_re.index[0].month
        startday  = df_CLIM_re.index[0].day
        startstring = '{}/{}/{}'.format(startday,startmonth,startyear)

        msk = df_CLIM_re.decdate.values<spin_date_end+1
        spin_days = df_CLIM_re.decdate.values[msk]

        smb_spin = df_CLIM_re['BDOT'][msk].values
        tskin_spin = df_CLIM_re['TSKIN'][msk].values

        nu = len(spin_days)
        spin_days_all = np.zeros(len(sub)*nu)
        smb_spin_all = np.zeros_like(spin_days_all)
        tskin_spin_all = np.zeros_like(spin_days_all)

        spin_days_all = (sub[:,np.newaxis]+spin_days).flatten()
        spin_dict = {}
        for ID in df_CLIM_ids:
            spin_dict[ID] = np.tile(df_CLIM_re[ID][msk].values, len(sub))

        df_CLIM_decdate = df_CLIM_re.set_index('decdate')
        df_spin = pd.DataFrame(spin_dict,index = spin_days_all)
        df_spin.index.name = 'decdate'

        df_FULL = pd.concat([df_spin,df_CLIM_decdate])

        CD = {}
        CD['time'] = df_FULL.index
        for ID in df_CLIM_ids:
            if ID == 'TSKIN':
                CD[ID] = df_FULL[ID].values
            elif ID=='SRHO':
                CD[ID] = df_FULL[ID].values
            else:
                CD[ID] = df_FULL[ID].values * stepsperyear / 917

        SEBfluxes = None

    ##############################################
    ##############################################

    elif (not SEB and calc_melt): # calculate the melt flux based on energy fluxes from climate data, but SEB module in CFM will not run
        #(this is something of a pre-calculation of the melt.)

        drn = {'TS':'TSKIN','EVAP':'SUBLIM'} #customize this to change your dataframe column names to match the required inputs
        try:
            df_CLIM['RAIN'] = df_CLIM['PRECTOT'] - df_CLIM['PRECSNO']
            df_CLIM['BDOT'] = df_CLIM['PRECSNO'] #+ df_CLIM['EVAP']
            # df_CLIM['SUBLIM'] = df_CLIM[]

        except:
            pass
        df_CLIM.rename(mapper=drn,axis=1,inplace=True)
        try:
            df_CLIM.drop(['EVAP','PRECTOT','PRECSNO'],axis=1,inplace=True)
        except:
            pass
        #############

        df_CLIM['ALBEDO'] = df_CLIM['ALBEDO'].bfill()
        df_CLIM['ALBEDO'] = df_CLIM['ALBEDO'].ffill()

        SBC = 5.67e-8
        CP_I = 2097.0 
        m = 400*0.08
        dt = df_CLIM.index.to_series().diff().dt.total_seconds().mean()
        LF_I = 333500.0 #[J kg^-1]
        flux_df1 = ((df_CLIM['SW_d'] * (1 - df_CLIM['ALBEDO'])) + df_CLIM['LW_d'] + df_CLIM['QH'] + df_CLIM['QL']) #make sure that merra2 QH and QL are multiplied by -1 to make them 'into' the layer
        flux_df1_r = flux_df1.values#.reshape(flux_df1.shape[0],-1)
        # oshape = np.shape(flux_df1.values)
        
        Tcalc = np.zeros_like(flux_df1_r)
        meltmass = np.zeros_like(flux_df1_r)

        dts = df_CLIM.TSKIN.values
        # dts_r = dts.reshape(dts.shape[0],-1)
        # dsha = dts_r.shape[-1]

        # tindex = df_CLIM.time.data
        tindex = df_CLIM.index
        for kk,mdate in enumerate(tindex): #loop through all time steps, calculate melt for all lat/lon pairs at that time
            pmat = np.zeros(5) # p matrix to put into FQS solver

            if kk==0:
                T_0 = dts[kk]
            else:
                T_0 = dts[kk-1]

            a = SBC * dt / (CP_I*m)
            b = 0
            c = 0
            d = 1
            e = -1 * (flux_df1_r[kk]*dt/(CP_I*m)+T_0)

            pmat[0] = a
            pmat[3] = d
            pmat[4] = e
            pmat[np.isnan(pmat)] = 0

            fqs = FQS()
            r = fqs.quartic_roots(pmat)
            Tnew = (r[((np.isreal(r)) & (r>0))].real)
            Tnew[np.isnan(e)] = np.nan

            if Tnew>=273.15:
                Tcalc[kk] = 273.15000000000000        
                meltmass[kk] = (flux_df1[kk] - SBC*273.15**4) / LF_I * dt #multiply by dt to put in units per time step

            else:
                Tcalc[kk] = Tnew
                meltmass[kk] = 0

        Tcalc_out = Tcalc
        meltmass_out = meltmass

        df_CLIM['Tcalc'] = Tcalc_out
        df_CLIM['meltmass'] = meltmass_out

        # df_CLIM.to_csv('melt_test.csv')
        df_CLIM.drop(['SMELT','TSKIN'],axis=1,inplace=True)
        
        df_CLIM['SMELT'] = meltmass_out
        df_CLIM['TSKIN'] = Tcalc_out

        #############
        l1 = df_CLIM.columns.values.tolist()
        l2 = ['SMELT','BDOT','RAIN','TSKIN','SUBLIM','SRHO']
        notin = list(np.setdiff1d(l1,l2))
        df_CLIM.drop(notin,axis=1,inplace=True)
        # df_BDOT = pd.DataFrame(df_CLIM.BDOT)
        df_TS = pd.DataFrame(df_CLIM.TSKIN)

        res_dict_all = {'SMELT':'sum','BDOT':'sum','RAIN':'sum','TSKIN':'mean','SUBLIM':'sum','SRHO':'mean'} # resample type for all possible variables
        res_dict = {key:res_dict_all[key] for key in df_CLIM.columns} # resample type for just the data types in df_CLIM

        # df_BDOT_re = df_BDOT.resample(timeres).sum()
        if Tinterp == 'mean':
            df_TS_re = df_TS.resample(timeres).mean()
        elif Tinterp == 'effective':
            df_TS_re = df_TS.resample(timeres).apply(effectiveT)
        elif Tinterp == 'weighted':
            df_TS_re = pd.DataFrame(data=(df_BDOT.BDOT*df_TS.TSKIN).resample(timeres).sum()/(df_BDOT.BDOT.resample(timeres).sum()),columns=['TSKIN'])
            # pass

        df_CLIM_re = df_CLIM.resample(timeres).agg(res_dict)
        df_CLIM_re.TSKIN = df_TS_re.TSKIN
        df_CLIM_ids = list(df_CLIM_re.columns)

        df_CLIM_re['decdate'] = [toYearFraction(qq) for qq in df_CLIM_re.index]
        # df_CLIM_re = df_CLIM_re.fillna(method='pad')
        df_CLIM_re = df_CLIM_re.ffill()

        # df_TS_re['decdate'] = [toYearFraction(qq) for qq in df_TS_re.index]
        # df_BDOT_re['decdate'] = [toYearFraction(qq) for qq in df_BDOT_re.index]
        # df_TS_re = df_TS_re.fillna(method='pad')

        stepsperyear = 1/(df_CLIM_re.decdate.diff().mean())


        if 'SUBLIM' not in df_CLIM_re:
            df_CLIM_re['SUBLIM'] = np.zeros_like(df_CLIM_re['BDOT'])

        BDOT_mean_IE = ((df_CLIM_re['BDOT']+df_CLIM_re['SUBLIM'])*stepsperyear/917).mean()
        T_mean = (df_TS_re['TSKIN']).mean()

        hh  = np.arange(0,501)
        age, rho = hla.hl_analytic(350,hh,T_mean,BDOT_mean_IE)    
        if not desired_depth:
            # desired_depth = hh[np.where(rho>=916)[0][0]]
            desired_depth = hh[np.where(rho>=rho_bottom)[0][0]]
            depth_S1 = hh[np.where(rho>=450)[0][0]]
            depth_S2 = hh[np.where(rho>=650)[0][0]]
        else:
            desired_depth = desired_depth
            depth_S1 = desired_depth * 0.5
            depth_S2 = desired_depth * 0.75
        
        #### Make spin up series ###
        RCI_length = spin_date_end-spin_date_st+1
        num_reps = int(np.round(desired_depth/BDOT_mean_IE/RCI_length))
        years = num_reps*RCI_length
        sub = np.arange(-1*years,0,RCI_length)
        startyear = int(df_CLIM_re.index[0].year + sub[0])
        startmonth = df_CLIM_re.index[0].month
        startday  = df_CLIM_re.index[0].day
        startstring = '{}/{}/{}'.format(startday,startmonth,startyear)

        msk = df_CLIM_re.decdate.values<spin_date_end+1
        spin_days = df_CLIM_re.decdate.values[msk]

        smb_spin = df_CLIM_re['BDOT'][msk].values
        tskin_spin = df_CLIM_re['TSKIN'][msk].values

        nu = len(spin_days)
        spin_days_all = np.zeros(len(sub)*nu)
        smb_spin_all = np.zeros_like(spin_days_all)
        tskin_spin_all = np.zeros_like(spin_days_all)

        spin_days_all = (sub[:,np.newaxis]+spin_days).flatten()
        spin_dict = {}
        for ID in df_CLIM_ids:
            spin_dict[ID] = np.tile(df_CLIM_re[ID][msk].values, len(sub))

        df_CLIM_decdate = df_CLIM_re.set_index('decdate')
        df_spin = pd.DataFrame(spin_dict,index = spin_days_all)
        df_spin.index.name = 'decdate'

        df_FULL = pd.concat([df_spin,df_CLIM_decdate])
        # df_FULL.to_csv('df_full_noSEB.csv')

        CD = {}
        CD['time'] = df_FULL.index
        for ID in df_CLIM_ids:
            if ID == 'TSKIN':
                CD[ID] = df_FULL[ID].values
            elif ID=='SRHO':
                CD[ID] = df_FULL[ID].values
            else:
                CD[ID] = df_FULL[ID].values * stepsperyear / 917

        SEBfluxes = None

    else: #SEB True - SEB module in CFM will run

        l1 = df_CLIM.columns.values.tolist()

        if 'SMELT' in l1:
            df_CLIM.drop(['SMELT'],axis=1,inplace=True)

        # df_TS = pd.DataFrame(df_CLIM.TSKIN)

        # res_dict_all = ({'SMELT':'sum','BDOT':'sum','RAIN':'sum','TSKIN':'mean','T2m':'mean',
        #                'ALBEDO':'mean','QL':'mean','QH':'mean','SUBLIM':'sum','SW_d':'mean'}) # resample type for all possible variables
        
        # res_dict_all = ({'BDOT':'sum','RAIN':'sum','TSKIN':'mean','T2m':'mean',
        #                'ALBEDO':'mean','QL':'sum','QH':'sum','SUBLIM':'sum','SW_d':'sum','LW_d':'sum'}) # resample type for all possible variables

        res_dict_all = ({'BDOT':'sum','RAIN':'sum','TSKIN':'mean','T2m':'mean',
                       'ALBEDO':'mean','QL':'mean','QH':'mean','SUBLIM':'sum','SW_d':'mean','LW_d':'mean','LW_u':'mean'}) # resample type for all possible variables

        res_dict = {key:res_dict_all[key] for key in df_CLIM.columns} # resample type for just the data types in df_CLIM

        df_CLIM_re = df_CLIM.resample(timeres).agg(res_dict) #Energy fluxes remain W/m2, mass fluxes are in /time step
        df_CLIM_ids = list(df_CLIM_re.columns)

        df_CLIM_re['decdate'] = [toYearFraction(qq) for qq in df_CLIM_re.index]
        # df_CLIM_re = df_CLIM_re.fillna(method='pad')
        df_CLIM_re = df_CLIM_re.ffill()

        df_CLIM_seb = df_CLIM[res_dict.keys()]
        df_CLIM_seb.drop(['BDOT','RAIN'],axis=1)
        df_CLIM_seb_ids = list(df_CLIM_seb.columns)
        df_CLIM_seb['decdate'] = [toYearFraction(qq) for qq in df_CLIM_seb.index]

        dtRATIO = df_CLIM_re.index.to_series().diff().mean().total_seconds()/df_CLIM_seb.index.to_series().diff().mean().total_seconds()

        stepsperyear = 1/(df_CLIM_re.decdate.diff().mean())
        stepsperyear_seb = 1/(df_CLIM_seb.decdate.diff().mean())

        BDOT_mean_IE = (df_CLIM_re['BDOT']*stepsperyear/917).mean()
        
        try:
            T_mean = (df_CLIM_re['TSKIN']).mean()
        except:
            T_mean = (df_CLIM_re['T2m']).mean()

        hh  = np.arange(0,501)
        age, rho = hla.hl_analytic(350,hh,T_mean,BDOT_mean_IE)

        if not desired_depth:
            # desired_depth = hh[np.where(rho>=916)[0][0]]
            desired_depth = hh[np.where(rho>=rho_bottom)[0][0]]
            depth_S1 = hh[np.where(rho>=450)[0][0]]
            depth_S2 = hh[np.where(rho>=650)[0][0]]
        else:
            desired_depth = desired_depth
            depth_S1 = desired_depth * 0.5
            depth_S2 = desired_depth * 0.75

        #### Make spin up series ###
        RCI_length = spin_date_end-spin_date_st+1
        num_reps = int(np.round(desired_depth/BDOT_mean_IE/RCI_length))
        years = num_reps*RCI_length
        sub = np.arange(-1*years,0,RCI_length)
        startyear = int(df_CLIM_re.index[0].year + sub[0])
        startmonth = df_CLIM_re.index[0].month
        startday  = df_CLIM_re.index[0].day
        startstring = '{}/{}/{}'.format(startday,startmonth,startyear)

        msk = df_CLIM_re.decdate.values<spin_date_end+1
        spin_days = df_CLIM_re.decdate.values[msk]

        msk_seb = df_CLIM_seb.decdate.values<spin_date_end+1
        spin_days_seb = df_CLIM_seb.decdate.values[msk_seb]

        # smb_spin = df_CLIM_re['BDOT'][msk].values
        # tskin_spin = df_CLIM_re['TSKIN'][msk].values

        nu = len(spin_days)
        spin_days_all = np.zeros(len(sub)*nu)

        nu_seb = len(spin_days_seb)
        spin_days_all_seb = np.zeros(len(sub)*nu_seb)

        spin_days_all = (sub[:,np.newaxis]+spin_days).flatten()
        spin_dict = {}
        for ID in df_CLIM_ids:
            spin_dict[ID] = np.tile(df_CLIM_re[ID][msk].values, len(sub))

        spin_days_all_seb = (sub[:,np.newaxis]+spin_days_seb).flatten()
        spin_dict_seb = {}
        for ID in df_CLIM_seb_ids:
            spin_dict_seb[ID] = np.tile(df_CLIM_seb[ID][msk_seb].values, len(sub))

        df_CLIM_decdate = df_CLIM_re.set_index('decdate')
        df_spin = pd.DataFrame(spin_dict,index = spin_days_all)
        df_spin.index.name = 'decdate'

        df_CLIM_seb_decdate = df_CLIM_seb.set_index('decdate')
        df_spin_seb = pd.DataFrame(spin_dict_seb,index = spin_days_all_seb)
        df_spin_seb.index.name = 'decdate'

        df_FULL = pd.concat([df_spin,df_CLIM_decdate])

        # df_FULL.to_csv('df_full_SEB.csv')

        df_FULL_seb = pd.concat([df_spin_seb,df_CLIM_seb_decdate])

        CD = {}
        CD['time'] = df_FULL.index
        massIDs = ['SMELT','BDOT','RAIN','SUBLIM','EVAP']
        for ID in df_CLIM_ids:
            if ID not in massIDs:
                CD[ID] = df_FULL[ID].values            
            else:
                CD[ID] = df_FULL[ID].values * stepsperyear / 917

        SEBfluxes = {}
        SEBfluxes['time'] = df_FULL_seb.index
        SEBfluxes['dtRATIO'] = int(dtRATIO)
        for ID in df_CLIM_seb_ids:
            if ID not in massIDs:
                SEBfluxes[ID] = df_FULL_seb[ID].values            
            else:
                SEBfluxes[ID] = df_FULL_seb[ID].values * stepsperyear_seb / 917


    return CD, stepsperyear, depth_S1, depth_S2, desired_depth, SEBfluxes

class FQS:
    '''
    Fast Quartic Solver: analytically solves quartic equations (needed to calculate melt)
    Takes methods from fqs package (@author: NKrvavica)
    full documentation: https://github.com/NKrvavica/fqs/blob/master/fqs.py
    '''
    def __init__(self):
        pass

    # @jit(nopython=True)``
    def single_quadratic(self, a0, b0, c0):
        ''' 
        Analytical solver for a single quadratic equation
        '''
        a, b = b0 / a0, c0 / a0

        # Some repating variables
        a0 = -0.5*a
        delta = a0*a0 - b
        sqrt_delta = cmath.sqrt(delta)

        # Roots
        r1 = a0 - sqrt_delta
        r2 = a0 + sqrt_delta

        return r1, r2


    # @jit(nopython=True)
    def single_cubic(self, a0, b0, c0, d0):
        ''' 
        Analytical closed-form solver for a single cubic equation
        '''
        a, b, c = b0 / a0, c0 / a0, d0 / a0

        # Some repeating constants and variables
        third = 1./3.
        a13 = a*third
        a2 = a13*a13
        sqr3 = math.sqrt(3)

        # Additional intermediate variables
        f = third*b - a2
        g = a13 * (2*a2 - b) + c
        h = 0.25*g*g + f*f*f

        def cubic_root(x):
            ''' Compute cubic root of a number while maintaining its sign'''
            if x.real >= 0:
                return x**third
            else:
                return -(-x)**third

        if f == g == h == 0:
            r1 = -cubic_root(c)
            return r1, r1, r1

        elif h <= 0:
            j = math.sqrt(-f)
            k = math.acos(-0.5*g / (j*j*j))
            m = math.cos(third*k)
            n = sqr3 * math.sin(third*k)
            r1 = 2*j*m - a13
            r2 = -j * (m + n) - a13
            r3 = -j * (m - n) - a13
            return r1, r2, r3

        else:
            sqrt_h = cmath.sqrt(h)
            S = cubic_root(-0.5*g + sqrt_h)
            U = cubic_root(-0.5*g - sqrt_h)
            S_plus_U = S + U
            S_minus_U = S - U
            r1 = S_plus_U - a13
            r2 = -0.5*S_plus_U - a13 + S_minus_U*sqr3*0.5j
            r3 = -0.5*S_plus_U - a13 - S_minus_U*sqr3*0.5j
            return r1, r2, r3


    # @jit(nopython=True)
    def single_cubic_one(self, a0, b0, c0, d0):
        ''' 
        Analytical closed-form solver for a single cubic equation
        '''
        a, b, c = b0 / a0, c0 / a0, d0 / a0

        # Some repeating constants and variables
        third = 1./3.
        a13 = a*third
        a2 = a13*a13

        # Additional intermediate variables
        f = third*b - a2
        g = a13 * (2*a2 - b) + c
        h = 0.25*g*g + f*f*f

        def cubic_root(x):
            ''' Compute cubic root of a number while maintaining its sign
            '''
            if x.real >= 0:
                return x**third
            else:
                return -(-x)**third

        if f == g == h == 0:
            return -cubic_root(c)

        elif h <= 0:
            j = math.sqrt(-f)
            k = math.acos(-0.5*g / (j*j*j))
            m = math.cos(third*k)
            return 2*j*m - a13

        else:
            sqrt_h = cmath.sqrt(h)
            S = cubic_root(-0.5*g + sqrt_h)
            U = cubic_root(-0.5*g - sqrt_h)
            S_plus_U = S + U
            return S_plus_U - a13


    # @jit(nopython=True)
    def single_quartic(self, a0, b0, c0, d0, e0):
        '''
        Analytical closed-form solver for a single quartic equation
        '''
        a, b, c, d = b0/a0, c0/a0, d0/a0, e0/a0

        # Some repeating variables
        a0 = 0.25*a
        a02 = a0*a0

        # Coefficients of subsidiary cubic euqtion
        p = 3*a02 - 0.5*b
        q = a*a02 - b*a0 + 0.5*c
        r = 3*a02*a02 - b*a02 + c*a0 - d

        # One root of the cubic equation
        z0 = self.single_cubic_one(1, p, r, p*r - 0.5*q*q)

        # Additional variables
        s = cmath.sqrt(2*p + 2*z0.real + 0j)
        if s == 0:
            t = z0*z0 + r
        else:
            t = -q / s

        # Compute roots by quadratic equations
        r0, r1 = self.single_quadratic(1, s, z0 + t)
        r2, r3 = self.single_quadratic(1, -s, z0 - t)

        return r0 - a0, r1 - a0, r2 - a0, r3 - a0


    def multi_quadratic(self, a0, b0, c0):
        ''' 
        Analytical solver for multiple quadratic equations
        '''
        a, b = b0 / a0, c0 / a0

        # Some repating variables
        a0 = -0.5*a
        delta = a0*a0 - b
        sqrt_delta = np.sqrt(delta + 0j)

        # Roots
        r1 = a0 - sqrt_delta
        r2 = a0 + sqrt_delta

        return r1, r2


    def multi_cubic(self, a0, b0, c0, d0, all_roots=True):
        '''
        Analytical closed-form solver for multiple cubic equations
        '''
        a, b, c = b0 / a0, c0 / a0, d0 / a0

        # Some repeating constants and variables
        third = 1./3.
        a13 = a*third
        a2 = a13*a13
        sqr3 = math.sqrt(3)

        # Additional intermediate variables
        f = third*b - a2
        g = a13 * (2*a2 - b) + c
        h = 0.25*g*g + f*f*f

        # Masks for different combinations of roots
        m1 = (f == 0) & (g == 0) & (h == 0)     # roots are real and equal
        m2 = (~m1) & (h <= 0)                   # roots are real and distinct
        m3 = (~m1) & (~m2)                      # one real root and two complex

        def cubic_root(x):
            ''' Compute cubic root of a number while maintaining its sign
            '''
            root = np.zeros_like(x)
            positive = (x >= 0)
            negative = ~positive
            root[positive] = x[positive]**third
            root[negative] = -(-x[negative])**third
            return root

        def roots_all_real_equal(c):
            ''' Compute cubic roots if all roots are real and equal
            '''
            r1 = -cubic_root(c)
            if all_roots:
                return r1, r1, r1
            else:
                return r1

        def roots_all_real_distinct(a13, f, g, h):
            ''' Compute cubic roots if all roots are real and distinct
            '''
            j = np.sqrt(-f)
            k = np.arccos(-0.5*g / (j*j*j))
            m = np.cos(third*k)
            r1 = 2*j*m - a13
            if all_roots:
                n = sqr3 * np.sin(third*k)
                r2 = -j * (m + n) - a13
                r3 = -j * (m - n) - a13
                return r1, r2, r3
            else:
                return r1

        def roots_one_real(a13, g, h):
            ''' Compute cubic roots if one root is real and other two are complex
            '''
            sqrt_h = np.sqrt(h)
            S = cubic_root(-0.5*g + sqrt_h)
            U = cubic_root(-0.5*g - sqrt_h)
            S_plus_U = S + U
            r1 = S_plus_U - a13
            if all_roots:
                S_minus_U = S - U
                r2 = -0.5*S_plus_U - a13 + S_minus_U*sqr3*0.5j
                r3 = -0.5*S_plus_U - a13 - S_minus_U*sqr3*0.5j
                return r1, r2, r3
            else:
                return r1

        # Compute roots
        if all_roots:
            roots = np.zeros((3, len(a))).astype(complex)
            roots[:, m1] = roots_all_real_equal(c[m1])
            roots[:, m2] = roots_all_real_distinct(a13[m2], f[m2], g[m2], h[m2])
            roots[:, m3] = roots_one_real(a13[m3], g[m3], h[m3])
        else:
            roots = np.zeros(len(a))  # .astype(complex)
            roots[m1] = roots_all_real_equal(c[m1])
            roots[m2] = roots_all_real_distinct(a13[m2], f[m2], g[m2], h[m2])
            roots[m3] = roots_one_real(a13[m3], g[m3], h[m3])

        return roots


    def multi_quartic(self, a0, b0, c0, d0, e0):
        ''' 
        Analytical closed-form solver for multiple quartic equations
        '''
        a, b, c, d = b0/a0, c0/a0, d0/a0, e0/a0

        # Some repeating variables
        a0 = 0.25*a
        a02 = a0*a0

        # Coefficients of subsidiary cubic euqtion
        p = 3*a02 - 0.5*b
        q = a*a02 - b*a0 + 0.5*c
        r = 3*a02*a02 - b*a02 + c*a0 - d

        # One root of the cubic equation
        z0 = self.multi_cubic(1, p, r, p*r - 0.5*q*q, all_roots=False)

        # Additional variables
        s = np.sqrt(2*p + 2*z0.real + 0j)
        t = np.zeros_like(s)
        mask = (s == 0)
        t[mask] = z0[mask]*z0[mask] + r[mask]
        t[~mask] = -q[~mask] / s[~mask]

        # Compute roots by quadratic equations
        r0, r1 = self.multi_quadratic(1, s, z0 + t) - a0
        r2, r3 = self.multi_quadratic(1, -s, z0 - t) - a0

        return r0, r1, r2, r3


    def cubic_roots(self, p):
        '''
        A caller function for a fast cubic root solver (3rd order polynomial).
        '''
        # Convert input to array (if input is a list or tuple)
        p = np.asarray(p)

        # If only one set of coefficients is given, add axis
        if p.ndim < 2:
            p = p[np.newaxis, :]

        # Check if four coefficients are given
        if p.shape[1] != 4:
            raise ValueError('Expected 3rd order polynomial with 4 '
                             'coefficients, got {:d}.'.format(p.shape[1]))

        if p.shape[0] < 100:
            roots = [self.single_cubic(*pi) for pi in p]
            return np.array(roots)
        else:
            roots = self.multi_cubic(*p.T)
            return np.array(roots).T


    def quartic_roots(self, p):
        '''
        A caller function for a fast quartic root solver (4th order polynomial).
        '''
        # Convert input to an array (if input is a list or tuple)
        p = np.asarray(p)

        # If only one set of coefficients is given, add axis
        if p.ndim < 2:
            p = p[np.newaxis, :]

        # Check if all five coefficients are given
        if p.shape[1] != 5:
            raise ValueError('Expected 4th order polynomial with 5 '
                             'coefficients, got {:d}.'.format(p.shape[1]))

        if p.shape[0] < 100:
            roots = [self.single_quartic(*pi) for pi in p]
            return np.array(roots)
        else:
            roots = self.multi_quartic(*p.T)
            return np.array(roots).T