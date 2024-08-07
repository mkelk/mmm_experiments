from abc import ABC, abstractmethod
import pandas as pd
from matplotlib import pyplot as plt
import seaborn as sns

import pymc as pm
import arviz as az

from pymc_marketing.mmm.transformers import geometric_adstock, logistic_saturation


class MMM():
    def __init__(self, data, channelnames):
        self.data_raw = data
        self.modelname: str # must be set in subclass
        self.channelnames = channelnames
        self.salesname = 'sales'
        self.datename = 'date'
        self.dates = self.data_raw[self.datename]
        self.channelscale = 0
        self.salesscale = 0
        self.data_scaled = None
        self.model: pm.Model = None
        self.set_scaling()

    def set_scaling(self):
        """
        Set the scaling of the channels and sales to get more normalized data
        """
        self.data_scaled = self.data_raw.copy()

        self.channelscale = 0
        for channel in self.channelnames:
            if self.data_raw[channel].median() > self.channelscale:
                self.channelscale = self.data_raw[channel].median()
        for channel in self.channelnames:
            self.data_scaled[channel] = self.data_scaled[channel] / self.channelscale
        
        self.salesscale = self.data_scaled[self.salesname].median()
        self.data_scaled[self.salesname] = self.data_scaled[self.salesname] / self.salesscale

    def define_model(self):
        """
        Define the model based on the modelname
        """
        raise NotImplementedError("Subclass must implement abstract method 'define_model()")


    def fit(self, progressbar=True):
        """
        Fit the model
        """
        if self.model is None:
            self.define_model()        
        with self.model:
            self.idata = pm.sample(progressbar=progressbar)


    def get_back_scaled_idata(self):
        raise NotImplementedError("Subclass must implement abstract method 'get_back_scaled_idata()")

    def plot_posterior_predictive(self, original_scale=True, plot_kwargs=None):
        """
        Plot the posterior predictive of sales
        """
        plotvar = "mu_y"
        with self.model:
            pp = pm.sample_posterior_predictive(self.idata, var_names=[plotvar])
        
        plotdata = { 
            "date" : self.dates }
        
        scale = self.salesscale if original_scale else 1.0


        plotdata[plotvar] = pp.posterior_predictive[plotvar].mean(dim=["chain", "draw"]) * scale

        plotdata_df = pd.DataFrame(data=plotdata)
        fig, ax = plt.subplots()
        fig.set_size_inches(12, 6)
        tmpdata = self.data_scaled if not original_scale else self.data_raw
        sns.lineplot(x="date", y="sales", color="black", data=tmpdata, ax=ax)
        ax.set(title="Sales (Target Variable)", xlabel="date", ylabel="sales (scaled)" if not original_scale else "sales");
        sns.lineplot(x="date", y="mu_y", color="blue", data=plotdata_df, ax=ax)  

        for hdi_prob, alpha in zip((0.94, 0.50), (0.2, 0.4), strict=True):
            likelihood_hdi: DataArray = az.hdi(
                ary=pp.posterior_predictive * scale, hdi_prob=hdi_prob
            )["mu_y"]

            ax.fill_between(
                x=self.dates,
                y1=likelihood_hdi[:, 0],
                y2=likelihood_hdi[:, 1],
                color="C0",
                alpha=alpha,
                label=f"${100 * hdi_prob}\%$ HDI",  
            )

    def plot_parm_dist(self, original_scale=True):
        az.plot_posterior(
            self.get_back_scaled_idata(),
            var_names=self.fittedparmnames,
            figsize=(12, 6),
        )
        plt.tight_layout();



class MMMChannelsStraight(MMM):
    def __init__(self, data, channelnames, allowIntercept: bool = False, 
                 allowAdstockAndSat: bool = False, adstock_max_lag: int = 6):
        super().__init__(data, channelnames)
        self.modelname = 'google_fb_straight'
        self.fittedparmnames = ['beta', 'sigma']
        self.set_scaling()
        self.allowIntercept = allowIntercept
        self.allowAdstockAndSat = allowAdstockAndSat
        self.adstock_max_lag = adstock_max_lag
  

    def get_back_scaled_idata(self):
        """
        Get the back-scaled idata
        """
        back_scaled_idata = self.idata.copy()
        if self.allowIntercept:
            back_scaled_idata.posterior["intercept"] = back_scaled_idata.posterior["intercept"] * self.salesscale
        back_scaled_idata.posterior["sigma"] = back_scaled_idata.posterior["sigma"] * self.channelscale
        back_scaled_idata.posterior["beta"] = back_scaled_idata.posterior["beta"] * self.salesscale / self.channelscale 
        return back_scaled_idata


    def define_model(self):
        """
        Define the model
        """
        coords = { self.datename: self.dates, "channels": self.channelnames }
        
        with pm.Model(coords=coords) as self.model:
            # variables
            spend = pm.Data('spend', self.data_scaled[self.channelnames].values, dims=(self.datename, "channels"))

            # Priors
            if self.allowIntercept:
                intercept = pm.Normal('intercept', mu=1, sigma=5)
                if 'intercept' not in self.fittedparmnames:
                    self.fittedparmnames = self.fittedparmnames + ['intercept']
            else:
                intercept = 0

            sigma = pm.HalfNormal('sigma')

            # channel effects
            beta = pm.Normal('beta', mu=1, sigma=5, dims=("channels"))

            # adstock and saturation?
            if self.allowAdstockAndSat:
                alpha = pm.Beta('alpha', alpha=1, beta=3, dims=("channels"))
                self.fittedparmnames = self.fittedparmnames + ['alpha']

                lam = pm.Gamma('lam', alpha=3, beta=1, dims=("channels"))                
                self.fittedparmnames = self.fittedparmnames + ['lam']

                channel_adstock = pm.Deterministic(
                    name="channel_adstock",
                    var=geometric_adstock(
                        x=spend,
                        alpha=alpha,
                        l_max=self.adstock_max_lag,
                        normalize=True,
                        axis=0,
                    ),
                    dims=("date", "channels"),
                )

                channel_adstock_saturated = pm.Deterministic(
                    name="channel_adstock_saturated",
                    var=logistic_saturation(x=channel_adstock, lam=lam),
                    dims=("date", "channels"),
                )
                # Expected value from channels
                mu_channels = pm.Deterministic('mu_channels', pm.math.dot(channel_adstock_saturated, beta), dims=(self.datename))
            else:
                # Expected value from channels
                mu_channels = pm.Deterministic('mu_channels', pm.math.dot(spend, beta), dims=(self.datename))

            # Expected value
            mu_y = pm.Deterministic('mu_y', intercept + mu_channels, dims=(self.datename))


            # Likelihood
            y = pm.Normal('y', mu=mu_y, sigma=sigma, observed=self.data_scaled[self.salesname].values, dims=(self.datename))






class MMMChannelsStraightConfounder(MMM):
    def __init__(self, data, channelnames):
        super().__init__(data, channelnames)
        self.modelname = 'google_fb_straight'
        self.fittedparmnames = ['beta', 'sigma']
  

    def get_back_scaled_idata(self):
        """
        Get the back-scaled idata
        """
        back_scaled_idata = self.idata.copy()
        back_scaled_idata.posterior["intercept"] = back_scaled_idata.posterior["intercept"] * self.salesscale
        back_scaled_idata.posterior["sigma"] = back_scaled_idata.posterior["sigma"] * self.channelscale
        back_scaled_idata.posterior["beta_google"] = back_scaled_idata.posterior["beta_google"] * self.salesscale / self.channelscale 
        back_scaled_idata.posterior["beta_fb"] = back_scaled_idata.posterior["beta_fb"] * self.salesscale / self.channelscale
        back_scaled_idata.posterior["spend_google_0"] = back_scaled_idata.posterior["spend_google_0"] * self.channelscale
        back_scaled_idata.posterior["sigma_google"] = back_scaled_idata.posterior["sigma_google"] * self.channelscale
        return back_scaled_idata


    def define_model(self):
        """
        Define the model
        """
        coords = { self.datename: self.dates }
        
        with pm.Model(coords=coords) as self.model:
            # self.fittedparmnames = ['beta_fb', 'beta_google', 'beta_fb_google', 'sigma']
            self.fittedparmnames = ['beta_fb', 'beta_google', 'beta_fb_google', 'spend_google_0', 'sigma', 'intercept', 'sigma_google']
            # variables
            spend_fb = pm.Data('spend_fb', self.data_scaled['spend_fb'].values, dims=(self.datename))

            sigma = pm.HalfNormal('sigma')

            # intercept
            intercept = pm.Normal('intercept', mu=1, sigma=1)

            # channel effects
            beta_fb = pm.Normal('beta_fb', mu=1, sigma=1)
            beta_google = pm.Normal('beta_google', mu=1, sigma=1)

            # fb contribution
            mu_fb = pm.Deterministic('mu_fb', beta_fb*spend_fb, dims=(self.datename))

            # google contribution
            beta_fb_google = pm.Normal('beta_fb_google', mu=1, sigma=1)
            spend_google_0 = pm.Normal('spend_google_0', mu=1, sigma=1)
            spend_google_fb = pm.Deterministic('spend_google_fb', spend_fb * beta_fb_google, dims=(self.datename))

            sigma_google = pm.HalfNormal('sigma_google')

            spend_google = pm.Normal('spend_google', mu=spend_google_0 + spend_google_fb, sigma=sigma_google, dims=(self.datename), 
                                            observed=self.data_scaled['spend_google'].values)

            mu_google = pm.Deterministic('mu_google', beta_google*spend_google, dims=(self.datename))


            mu_channels = pm.Deterministic('mu_channels', intercept + mu_fb + mu_google, dims=(self.datename))

            # Expected value
            mu_y = pm.Deterministic('mu_y', intercept + mu_channels, dims=(self.datename))


            # Likelihood
            y = pm.Normal('y', mu=mu_y, sigma=sigma, observed=self.data_scaled[self.salesname].values, dims=(self.datename))





class MMMFbGoogleMetrics(MMM):
    def __init__(self, data, channelnames, fb_metric = "clicks_fb", google_metric = "clicks_google"):
        super().__init__(data, channelnames)
        self.modelname = 'google_fb_straight'
        self.fb_metric = fb_metric
        self.google_metric = google_metric
        self.fittedparmnames = ['beta_fb', 'beta_google', 'intercept', 'sigma']


    def get_back_scaled_idata(self):
        """
        Get the back-scaled idata
        """
        back_scaled_idata = self.idata.copy()
        back_scaled_idata.posterior["intercept"] = back_scaled_idata.posterior["intercept"] * self.salesscale
        back_scaled_idata.posterior["sigma"] = back_scaled_idata.posterior["sigma"] * self.channelscale
        back_scaled_idata.posterior["beta_fb"] = back_scaled_idata.posterior["beta_fb"] * self.salesscale 
        back_scaled_idata.posterior["beta_google"] = back_scaled_idata.posterior["beta_google"] * self.salesscale
        return back_scaled_idata


    def define_model(self):
        """
        Define the model
        """
        coords = { self.datename: self.dates }

        with pm.Model(coords=coords) as self.model:
            # variables
            fb_in = pm.Data('fb_in', self.data_scaled[self.fb_metric].values, dims=(self.datename))
            google_in = pm.Data('google_in', self.data_scaled[self.google_metric].values, dims=(self.datename))

            # Priors
            intercept = pm.Normal('intercept', mu=1, sigma=1)

            sigma = pm.HalfNormal('sigma')

            # channel effects
            beta_fb = pm.Normal('beta_fb', mu=1, sigma=1)
            beta_google = pm.Normal('beta_google', mu=1, sigma=1)

            mu_channels = pm.Deterministic('mu_channels', fb_in*beta_fb + google_in*beta_google, dims=(self.datename))

            # Expected value
            mu_y = pm.Deterministic('mu_y', intercept + mu_channels, dims=(self.datename))


            # Likelihood
            y = pm.Normal('y', mu=mu_y, sigma=sigma, observed=self.data_scaled[self.salesname].values, dims=(self.datename))



