import numpy as np
from astropy import units as u
from astropy.coordinates import SkyCoord, EarthLocation, AltAz
from numba import njit, jit
from poliastro.core.propagation import markley, vallado, pimienta, gooding, danby, farnocchia,  mikkola, func_twobody
from poliastro.core.elements import coe2rv
from envs.transformations import ecef2aer, uvw2aer, aer2uvw
from poliastro.bodies import Earth
import functools
from astropy import _erfa as erfa
from scipy.integrate import DOP853, solve_ivp
from scipy.linalg import cholesky
import pymap3d as pm

k = Earth.k.to_value(u.m**3/u.s**2)
RE_mean = Earth.R_mean.to_value(u.m)
RE_eq = Earth.R.to_value(u.m)


@njit
def ad_none(t0, u_, k_):
    return 0, 0, 0

#  The main purpose of poliastro.core.propagation is to propagate a body along its orbit.

########################################################################################################################
#  Calculate fx - state transition function - predict next state based on constant velocity model x = vt + x_0
########################################################################################################################

@njit
def fx_xyz_markley(x, dt, k=k):
    """
    desc: Solves the kepler problem by a non iterative method. Relative error is around 1e-18, only limited by machine double-precission errors.
    :param x: Position and velocity Vector
    :param dt: Time of flight
    :param k: Standard gravitational parameter of the attractor.
    :return: Propogated position  and velocity vectors(1x3)
    """
    r0 = x[:3]
    v0 = x[3:]
    tof = dt
    rv = markley(k, r0, v0, tof) # (m^3 / s^2), (m), (m/s), (s)
    x_post = np.zeros(6)
    x_post[:3] = rv[0]
    x_post[3:] = rv[1]
    return x_post


@njit
def fx_xyz_vallado(x, dt, k = k, numiter=350):
    """
    desc: Solves Kepler’s Equation by applying a Newton-Raphson method.
    :param x: Position and velocity Vector
    :param dt: Array of times to propagate.
    :param k: Standard gravitational parameter of the attractor.
    :param numiter: Number of iterations
    :return: Propogated position  and velocity vectors(1x3)
    """
    r0 = x[:3]
    v0 = x[3:]
    tof = dt
    # Compute Lagrange coefficients
    f, g, fdot, gdot = vallado(k, r0, v0, tof, numiter)

    assert np.abs(f * gdot - fdot * g - 1) < 1e-5  # Fixed tolerance

    # Return position and velocity vectors
    r = f * r0 + g * v0
    v = fdot * r0 + gdot * v0
    x_post = np.zeros(6)
    x_post[:3] = r
    x_post[3:] = v
    return x_post


@njit
def fx_xyz_pimienta(x, dt, k=k):
    """
    desc: Raw algorithm for Adonis’ Pimienta and John L. Crassidis 15th order polynomial Kepler solver.
    :param x: Position and velocity Vector
    :param dt: time of flight
    :param k: Standard gravitational parameter of the attractor.
    :return: Propogated position  and velocity vectors(1x3)
    """
    r0 = x[:3]
    v0 = x[3:]
    tof = dt
    rv = pimienta(k, r0, v0, tof)
    x_post = np.zeros(6)
    x_post[:3] = rv[0]
    x_post[3:] = rv[1]
    return x_post


@njit
def fx_xyz_gooding(x, dt, k=k, numiter=150, rtol=1e-8):
    """
    desc :Solves the Elliptic Kepler Equation with a cubic convergence and accuracy better than 10e-12 rad is normally achieved.
    It is not valid for eccentricities equal or greater than 1.0.
    :param x: Position and velocity Vector.
    :param dt: Time of flight
    :param k: Standard gravitational parameter of the attractor.
    :param numiter: number of iterations
    :param rtol: Relative error for accuracy of the method.
    :return: Propogated position  and velocity vectors(1x3)
    """
    r0 = x[:3]
    v0 = x[3:]
    tof = dt
    rv = gooding(k, r0, v0, tof, numiter=numiter, rtol=rtol)
    x_post = np.zeros(6)
    x_post[:3] = rv[0]
    x_post[3:] = rv[1]
    return x_post


@njit
def fx_xyz_danby(x, dt, k=k):
    """
    desc: Kepler solver for both elliptic and parabolic orbits based on Danby’s algorithm.
    :param x: Position and velocity Vector
    :param dt: Array of times to propagate.
    :param k: Standard gravitational parameter of the attractor.
    :return: Propogated position  and velocity vectors(1x3)
    """
    r0 = x[:3]
    v0 = x[3:]
    tof = dt
    rv = danby(k, r0, v0, tof)
    x_post = np.zeros(6)
    x_post[:3] = rv[0]
    x_post[3:] = rv[1]
    return x_post


@njit
def fx_xyz_farnocchia(x, dt, k=k):
    """
    Desc: Propagates orbit using mean motion.
    :param x: Position and velocity Vector
    :param dt: Array of times to propagate.
    :param k: Standard gravitational parameter of the attractor.
    :return: Propogated position  and velocity vectors(1x3)
    """
    x_post = np.reshape(farnocchia(k, x[:3], x[3:], dt), 6)
    return x_post


@njit
def fx_xyz_mikkola(x, dt, k=k):
    """
    desc: Raw algorithm for Mikkola’s Kepler solver.
    :param x: Position and velocity Vector
    :param dt: Array of times to propagate.
    :param k: Standard gravitational parameter of the attractor.
    :return: Propogated position  and velocity vectors(1x3)
    """
    r0 = x[:3]
    v0 = x[3:]
    tof = dt
    rv = mikkola(k, r0, v0, tof)
    x_post = np.zeros(6)
    x_post[:3] = rv[0]
    x_post[3:] = rv[1]
    return x_post


def fx_xyz_cowell(x, dt, k=k, rtol=1e-11, *, events=None, ad=ad_none, **ad_kwargs):
    """
    desc: Differential equation for the initial value two body problem.This function follows Cowell’s formulation.
    :param x: Six component state vector [x, y, z, vx, vy, vz] (km, km/s).
    :param dt: Array of times to propagate.
    :param k: Standard gravitational parameter of the attractor.
    :param rtol:
    :param events: events
    :param ad: Non Keplerian acceleration (km/s2).
    :param ad_kwargs: perturbation parameters passed to ad.
    :return: Propogated position  and velocity vectors(1x3)
    """
    u0 = x
    tof = dt
    f_with_ad = functools.partial(func_twobody, k=k, ad=ad, ad_kwargs=ad_kwargs)
    # solving differential equation
    result = solve_ivp(
        f_with_ad,              # function fun(t,y)
        (0, tof),               # interval of integration
        u0,                     # initial state
        rtol=rtol,              # relative tolerance
        atol=1e-12,             # absolute tolerance
        method=DOP853,          # explicit Rung-Kutta method of order 8
        dense_output=True,      # whether to compute a continuous solution
        events=events,
    )
    if not result.success:
        raise RuntimeError("Integration failed")

    t_end = (
        min(result.t_events[0]) if result.t_events and len(result.t_events[0]) else None
    )

    return result.sol(tof) # ODE solution for the time instance

########################################################################################################################
#  Calculate hx - measurement function - convert state into a measurement where measurements are [x_pos, y_pos]
########################################################################################################################

def hx_xyz(x_gcrs, trans_matrix=None, observer_lla=None, observer_itrs=None, time=None):
    """
    desc: measurement function - convert state into a measurement where measurements are [azimuth, elevation]
    :param x_gcrs:          means for all objects at each time step
    :param trans_matrix:    Transition matrix
    :param observer_lla:    Observer coordinates
    :param observer_itrs:   Observer coordinates in itrs (meters)
    :param time:
    :return:
    """
    return x_gcrs[:3]

def hx_aer_erfa(x_gcrs, trans_matrix, observer_lla, observer_itrs, time=None):
    """
    desc: measurement function - convert state into a measurement where measurements are [azimuth, elevation]
    :param x_gcrs:          means for all objects at each time step
    :param trans_matrix:    Transition matrix
    :param observer_lla:    Observer coordinates
    :param observer_itrs:   Observer coordinates in itrs (meters)
    :param time:
    :return: array[azimuth, elevation, slant]
    """
    x_itrs = trans_matrix @ x_gcrs[:3]
    aer = ecef2aer(observer_lla, x_itrs, observer_itrs)
    return aer


def hx_aer_astropy(x_gcrs, time, observer_lla=None, trans_matrix=None, observer_itrs=None):
    """
    desc: measurement function - convert state into a measurement where measurements are [azimuth, elevation]
    :param x_gcrs:
    :param time:
    :param observer_lla:
    :param trans_matrix:
    :param observer_itrs:   Observer coordinates in itrs (meters)
    :return: array[azimuth, elevation, slant]
    """
    object = SkyCoord(x=x_gcrs[0] * u.m, y=x_gcrs[1] * u.m, z=x_gcrs[2] * u.m, frame='gcrs',
                   representation_type='cartesian', obstime=time)
    if observer_itrs is None:
        obs = EarthLocation.from_geodetic(lat=observer_lla[0] * u.rad, lon=observer_lla[1] * u.rad, height=observer_lla[2] * u.m)
    else:
        obs = EarthLocation.from_geocentric(x=observer_itrs[0] * u.m, y=observer_itrs[1] * u.m, z=observer_itrs[2] * u.m)
    AltAz_frame = AltAz(obstime=time, location=obs)
    results = object.transform_to(AltAz_frame)

    az = results.az.to_value(u.rad)
    alt = results.alt.to_value(u.rad)
    sr = results.distance.to_value(u.m)
    aer = np.array([az, alt, sr])
    return aer


@jit(['float64[::1](float64[::1],float64[::1])'])
def residual_z_aer(a, b):
    # prep array to receive results
    c = np.empty(a.shape)
    c[0] = np.arctan2(np.sin(a[0]-b[0]), np.cos(a[0]-b[0])) # azimuth: input range (0, 2pi), output range (-pi, pi)
    c[1] = (a[1] - b[1]) # elevation: input range (-pi/2, pi/2), output range (-pi, pi)
    c[2] = (a[2] - b[2])
    return c


@njit
def residual_xyz(a, b):
    c = np.subtract(a, b)
    return c

@njit
def mean_xyz(a, w):
    b = np.dot(w, a)
    return b


@njit
def mean_z_aer_depro(sigmas, Wm):
    # deprecated
    z = np.zeros(3)
    sum_sin_az, sum_cos_az, sum_sin_el, sum_cos_el = 0., 0., 0., 0.

    Wm = Wm/np.sum(Wm)

    for i in range(len(sigmas)):
        s = sigmas[i]
        sum_sin_az = sum_sin_az + np.sin(s[0])*Wm[i]
        sum_cos_az = sum_cos_az + np.cos(s[0])*Wm[i]
        sum_sin_el = sum_sin_el + np.sin(s[1]+np.pi)*Wm[i]
        sum_cos_el = sum_cos_el + np.cos(s[1]+np.pi)*Wm[i]
        z[2] = z[2] + s[2] * Wm[i]
    z[0] = np.arctan2(sum_sin_az, sum_cos_az)
    z[1] = np.arctan2(sum_sin_el, sum_cos_el)-np.pi
    while z[1] > np.pi or z[1] < -np.pi:
        z[1] = z[1] - np.sign(z[1])*np.pi*2
    if z[1] > np.pi/2 or z[1] < -np.pi/2:
        z[1] = np.sign(z[1])*np.pi - z[1]
        z[0] = z[0] - np.pi
    while z[0] > 2*np.pi or z[0] < 0:
        z[0] = z[0] - np.sign(z[0])*np.pi*2
    return z


def mean_z_unit_vector(sigmas, Wm):
    n, z_dim = sigmas.shape
    z = np.empty(3)
    sigmas_u = np.empty((n, z_dim-1))
    for i in range(len(sigmas)):
        sigmas_u[i] = erfa.s2c(sigmas[0, 0], sigmas[0, 1])[:2]
        
    z_mean_u = np.empty(z_dim)
    z_mean_u[:2] = np.average(sigmas_u, axis=0, weights=Wm)
    z_mean_u[2] = np.average(sigmas[:, 2], weights=Wm)
    z[:2] = erfa.c2s(z_mean_u)
    z[2] = z_mean_u[2]

    while z[1] > np.pi or z[1] < -np.pi:
        z[1] = z[1] - np.sign(z[1])*np.pi*2
    if z[1] > np.pi/2 or z[1] < -np.pi/2:
        z[1] = np.sign(z[1])*np.pi - z[1]
        z[0] = z[0] - np.pi
    while z[0] > 2*np.pi or z[0] < 0:
        z[0] = z[0] - np.sign(z[0])*np.pi*2
    return z


def mean_z_enu(sigmas, Wm):
    """
    desc:transforms points to east, north, up; takes cartesian mean of enu; transforms enu back to aer
    :param sigmas:
    :param Wm:
    :return:
    """
    enu_mean = np.average(np.array([pm.aer2enu(*sigma, deg=False) for sigma in sigmas]), axis=0, weights=Wm)
    return np.array(pm.enu2aer(*enu_mean, deg=False))


@njit
def mean_z_uvw(sigmas, Wm):
    """
    desc: transforms points to east, north, up; takes cartesian mean of enu; transforms enu back to aer
    :param sigmas:
    :param Wm:
    :return: modified array[azimuth, elevation, slant]
    """
    aers = np.empty(shape=sigmas.shape)
    for i in range(len(aers)):
        aers[i] = aer2uvw(sigmas[i])
    uvw_mean = np.dot(Wm, aers)
    return uvw2aer(uvw_mean)


def init_state_vec(orbits='Default', random_state=np.random.RandomState()):
    if orbits == 'Default':
        orbits = ['LEO', 'MEO', 'GEO', 'LEO', 'MEO', 'GEO', 'Tundra', 'Molniya']
    random_state.shuffle(orbits)
    exo_atmospheric = False

    inc = np.radians(random_state.uniform(0, 180)) # (rad) – Inclination
    raan = np.radians(random_state.uniform(0, 360)) # (rad) – Right ascension of the ascending node.
    argp = np.radians(random_state.uniform(low=0, high=360)) # (rad) – Argument of the pericenter.
    nu = np.radians(random_state.uniform(0, 360)) # (rad) – True anomaly.

    if orbits[-1] == 'LEO':
        while not exo_atmospheric:
            a = random_state.uniform(RE_eq+300*1000, RE_eq+2000*1000) # (m) – Semi-major axis.
            ecc = random_state.uniform(0, .25) # (Unit-less) – Eccentricity.
            b = a*np.sqrt(1-ecc**2)
            if b > RE_eq+300*1000:
                exo_atmospheric = True
    if orbits[-1] == 'MEO':
        while not exo_atmospheric:
            a = random_state.uniform(RE_eq+2000*1000, RE_eq+35786*1000) # (m) – Semi-major axis.
            ecc = random_state.uniform(0, .25) # (Unit-less) – Eccentricity.
            b = a*np.sqrt(1-ecc**2)
            if b > RE_eq+300*1000:
                exo_atmospheric = True
    if orbits[-1] == 'GEO':
        a = 42164*1000 # (m) – Semi-major axis.
        stationary = random_state.randint(0, 2)
        ecc = stationary*random_state.uniform(0, .25) # (Unit-less) – Eccentricity.
        inc = stationary*random_state.uniform(0, np.radians(0)) # (Unit-less) – Eccentricity.
    if orbits[-1] == 'Molniya':
        a = 26600*1000 # (m) – Semi-major axis.
        inc = np.radians(63.4) # (rad) – Inclination
        ecc = 0.737 # (Unit-less) – Eccentricity.
        argp = np.radians(270) # (rad) – Argument of the pericenter.
    if orbits[-1] == 'Tundra':
        a = 42164*1000 # (m) – Semi-major axis.
        inc = np.radians(63.4) # (rad) – Inclination
        ecc = 0.2 # (Unit-less) – Eccentricity.
        argp = np.radians(270) # (rad) – Argument of the pericenter.

    p = a*(1-ecc**2) # (km) - Semi-latus rectum or parameter
    return np.concatenate(coe2rv(k, p, ecc, inc, raan, argp, nu))


def robust_cholesky(a):
    " Cholesky function for Semi positive definite matrix "
    try:
        return cholesky(a)
    except:
        i = -6
        done = False
        while not done:
            e = np.eye(len(a))*10**i
            try:
                return cholesky(a+e)
            except:
                i += 1
            if i == 10:
                done = True
    raise np.linalg.LinAlgError

